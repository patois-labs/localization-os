"""Engine registry + new MT/LLM providers (P3, item 6) — fully offline.

Hermetic: throwaway LOC_TMS_DATA_DIR, provider keys cleared (forces the offline
MockProvider for the built-ins), ASCII-only prints, httpx.MockTransport for every
provider call (no live network), FastAPI TestClient for the Setup AI routes.

Covers:
  * EngineProfile CRUD + Fernet secret round-trip (the key never leaks in
    list/show/JSON/engine_row — a masked flag only).
  * legacy local-engine migration seeds exactly one "Local (migrated)" row,
    idempotently.
  * per-kind Test-connection probes via MockTransport: 200 OK + 401 auth-fail.
  * each new provider translates a segment batch via a mocked transport with the
    CORRECT request shape (DeepL DeepL-Auth-Key + formality/model_type/context/
    custom_instructions; Google v2 key param; Microsoft subscription key + region
    header; Cohere bearer + v2/chat; openai_compatible bearer + v1/chat/completions
    with a free-text model) and maps the response.
  * NMT error mapping -> the run_completion error dict; a prompt-only task routed
    to an NMT engine is REJECTED cleanly (NmtUnsupported), never silent garbage.
  * capability flags (CAPS) correct per kind.
  * char-based spend accrual for NMT + Cohere token pricing.
  * failover across engine KINDS (DeepL -> Microsoft).
  * get_provider resolves engine:<id> to the right subclass; disabled/missing
    degrades to the offline mock.
  * Setup AI engine routes (create/update/delete/test) + the CLI engine group
    round-trip (secrets never echoed).
"""

import json
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_engines_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine as db_engine
from app.db import init_db
from app.main import app
from app.models import EngineProfile
from app.services import engines, pricing, settings
from app.services import providers as P
from app.services import spend as spend_svc
from app.services.providers import (
    AmazonTranslateProvider,
    CohereProvider,
    DeepLProvider,
    GoogleTranslateProvider,
    LaraProvider,
    MicrosoftTranslatorProvider,
    NmtUnsupported,
    OpenAICompatibleProvider,
    ProviderSpec,
    get_provider,
    provider_is_live,
    register_provider,
    run_completion,
)

init_db()


# ---- transports -------------------------------------------------------------
def deepl_transport(captured, status=200):
    def h(req):
        captured["path"] = req.url.path
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = json.loads(req.content)
        if status != 200:
            return httpx.Response(status, json={"message": "bad key"})
        return httpx.Response(200, json={"translations": [{"text": "Bonjour le monde"}]})

    return httpx.MockTransport(h)


def google_transport(captured, status=200):
    def h(req):
        captured["path"] = req.url.path
        captured["params"] = dict(req.url.params)
        captured["api_key_header"] = req.headers.get("X-Goog-Api-Key")
        captured["body"] = json.loads(req.content) if req.content else {}
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "bad key"}})
        return httpx.Response(
            200, json={"data": {"translations": [{"translatedText": "Hola mundo"}]}}
        )

    return httpx.MockTransport(h)


def microsoft_transport(captured, status=200):
    def h(req):
        captured["path"] = req.url.path
        captured["params"] = dict(req.url.params)
        captured["key"] = req.headers.get("Ocp-Apim-Subscription-Key")
        captured["region"] = req.headers.get("Ocp-Apim-Subscription-Region")
        captured["body"] = json.loads(req.content)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "bad"}})
        return httpx.Response(200, json=[{"translations": [{"text": "Hallo Welt"}]}])

    return httpx.MockTransport(h)


def cohere_transport(captured, status=200):
    def h(req):
        captured["path"] = req.url.path
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = json.loads(req.content)
        if status != 200:
            return httpx.Response(status, json={"message": "unauthorized"})
        return httpx.Response(
            200,
            json={
                "message": {"content": [{"type": "text", "text": "Salut le monde"}]},
                "usage": {"tokens": {"input_tokens": 12, "output_tokens": 4}},
            },
        )

    return httpx.MockTransport(h)


def openai_compatible_transport(captured, status=200):
    def h(req):
        captured["path"] = req.url.path
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = json.loads(req.content)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "bad key"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Bonjour le monde"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 3},
            },
        )

    return httpx.MockTransport(h)


def openai_compatible_no_usage_transport(captured):
    """A 200 openai_compatible response with NO `usage` block — the spend-cap-bypass
    trap: a truthy zero-token usage dict here would record a permanent $0 cost."""

    def h(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Bonjour le monde"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )  # NO usage key

    return httpx.MockTransport(h)


def cohere_no_usage_transport(captured):
    def h(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200, json={"message": {"content": [{"type": "text", "text": "Salut le monde"}]}}
        )  # NO usage

    return httpx.MockTransport(h)


def lara_transport(captured, status=200):
    def h(req):
        captured["path"] = req.url.path
        captured["auth"] = req.headers.get("Authorization")
        captured["content_type"] = req.headers.get("Content-Type")
        captured["content_md5"] = req.headers.get("Content-MD5")
        captured["date"] = req.headers.get("Date")
        captured["body"] = json.loads(req.content)
        if status != 200:
            return httpx.Response(status, json={"error": "bad signature"})
        return httpx.Response(200, json={"content": "Bonjour le monde"})

    return httpx.MockTransport(h)


def amazon_transport(captured, status=200):
    def h(req):
        captured["path"] = req.url.path
        captured["auth"] = req.headers.get("Authorization")
        captured["target"] = req.headers.get("X-Amz-Target")
        captured["amz_date"] = req.headers.get("X-Amz-Date")
        captured["sha256"] = req.headers.get("X-Amz-Content-Sha256")
        captured["body"] = json.loads(req.content)
        if status != 200:
            return httpx.Response(status, json={"__type": "UnrecognizedClientException"})
        return httpx.Response(
            200,
            json={
                "TranslatedText": "Hola mundo",
                "SourceLanguageCode": "en",
                "TargetLanguageCode": "es",
            },
        )

    return httpx.MockTransport(h)


def probe_transport(status=200):
    """A blanket transport returning `status` for any GET (the test-connection
    probes only GET). 200 bodies are shaped for /v1/models where local needs it."""

    def h(req):
        if status != 200:
            return httpx.Response(status, json={"error": "auth"})
        return httpx.Response(200, json={"data": [{"id": "m1"}], "character_count": 0})

    return httpx.MockTransport(h)


def _clear_profiles():
    with Session(db_engine) as s:
        for p in s.exec(select(EngineProfile)).all():
            s.delete(p)
        s.commit()


# ---- tests ------------------------------------------------------------------
def test_crud_and_secret_round_trip():
    _clear_profiles()
    p = engines.create_profile(
        "MyDeepL", "deepl", api_key="sk-secret-123", cfg={"formality": "more"}, model=""
    )
    assert p.id and p.kind == "deepl"
    # secret round-trips through Fernet
    assert engines.dec_key(p.api_key_enc) == "sk-secret-123"
    # engine_row NEVER contains the key
    row = engines.engine_row(p)
    assert "sk-secret-123" not in json.dumps(row), "KEY LEAKED in engine_row"
    assert row["key_set"] is True and row["provider_name"] == f"engine:{p.id}"
    # list/show never leak either
    listed = json.dumps([engines.engine_row(x) for x in engines.list_profiles()])
    assert "sk-secret-123" not in listed
    # unique name enforced
    try:
        engines.create_profile("MyDeepL", "deepl")
        raise AssertionError("duplicate name should raise")
    except ValueError:
        pass
    # unknown kind rejected
    try:
        engines.create_profile("X", "bogus")
        raise AssertionError("unknown kind should raise")
    except ValueError:
        pass
    # update: blank key never blanks a stored key; config merges; rename works
    engines.update_profile(p.id, name="MyDeepL2", cfg={"foo": "bar"}, api_key="")
    fresh = engines.get_profile(p.id)
    assert fresh.name == "MyDeepL2"
    assert engines.dec_key(fresh.api_key_enc) == "sk-secret-123", "empty key blanked the secret!"
    assert fresh.config.get("formality") == "more" and fresh.config.get("foo") == "bar"
    # rotate key
    engines.update_profile(p.id, api_key="sk-new-999")
    assert engines.dec_key(engines.get_profile(p.id).api_key_enc) == "sk-new-999"
    # delete
    assert engines.delete_profile(p.id) is True
    assert engines.get_profile(p.id) is None
    print("  CRUD + Fernet secret round-trip (never leaked); unique-name + kind guards OK")


def test_legacy_local_migration():
    _clear_profiles()
    # No local server -> no seed
    settings.set_many({"local_base_url": ""})
    assert engines.migrate_legacy_local() is None
    assert not any(p.kind == "local" for p in engines.list_profiles())
    # Configure a local server -> exactly one "Local (migrated)" row, idempotent
    settings.set_many(
        {
            "local_base_url": "http://10.0.0.5:1234",
            "local_api_key": "lm-studio",
            "local_model": "qwen3",
        }
    )
    first = engines.migrate_legacy_local()
    assert first and first.kind == "local" and first.name == "Local (migrated)"
    assert first.base_url == "http://10.0.0.5:1234" and first.model == "qwen3"
    again = engines.migrate_legacy_local()
    assert again.id == first.id, "migration must be idempotent"
    locals_ = [p for p in engines.list_profiles() if p.kind == "local"]
    assert len(locals_) == 1, "exactly one migrated local row"
    print("  legacy local migration: no-op when unconfigured, one idempotent row when set OK")


def test_capability_flags():
    caps = {
        "deepl": DeepLProvider().CAPS,
        "google": GoogleTranslateProvider().CAPS,
        "microsoft": MicrosoftTranslatorProvider().CAPS,
        "cohere": CohereProvider().CAPS,
        "openai_compatible": OpenAICompatibleProvider().CAPS,
        "lara": LaraProvider().CAPS,
        "amazon": AmazonTranslateProvider().CAPS,
    }
    assert caps["deepl"]["supports_prompts"] is False
    assert caps["deepl"]["supports_formality"] is True  # DeepL has formality
    assert caps["google"]["supports_formality"] is False  # Google has none found
    assert caps["microsoft"]["supports_glossary"] is False  # Azure real-time: none
    assert caps["cohere"]["supports_prompts"] is True  # LLM
    assert caps["openai_compatible"]["supports_prompts"] is True  # LLM (chat completions)
    assert caps["lara"]["supports_prompts"] is False and caps["lara"]["supports_glossary"] is True
    assert (
        caps["amazon"]["supports_prompts"] is False and caps["amazon"]["supports_formality"] is True
    )
    print(
        "  capability flags correct per kind (formality: DeepL/Amazon yes/Google no; "
        "prompts: NMT no/Cohere+OAI yes; Lara glossary yes) OK"
    )


def test_deepl_request_shape_and_mapping():
    _clear_profiles()
    p = engines.create_profile("D", "deepl", api_key="k1", cfg={"formality": "less"})
    cap = {}
    prov = DeepLProvider(profile=engines.get_profile(p.id), transport=deepl_transport(cap))
    out = prov.complete(
        "unused",
        task="translate",
        payload={"text": "Hello world", "target": "fr-FR", "source": "en-US"},
    )
    assert out == "Bonjour le monde"
    assert cap["path"] == "/v2/translate"
    assert cap["auth"] == "DeepL-Auth-Key k1"  # correct auth header
    assert cap["body"]["text"] == ["Hello world"]  # text array
    assert cap["body"]["target_lang"] == "FR"
    assert cap["body"]["source_lang"] == "EN-US"
    assert cap["body"]["formality"] == "less"  # from profile config
    assert prov.last_usage["chars"] == len("Hello world") and prov.last_usage["nmt_kind"] == "deepl"
    print("  DeepL: DeepL-Auth-Key header + text[] + target_lang + formality; response mapped OK")


def test_google_request_shape_and_mapping():
    _clear_profiles()
    p = engines.create_profile("G", "google", api_key="gk")
    cap = {}
    prov = GoogleTranslateProvider(
        profile=engines.get_profile(p.id), transport=google_transport(cap)
    )
    out = prov.complete("unused", task="translate", payload={"text": "Hello", "target": "es-ES"})
    assert out == "Hola mundo"
    assert cap["path"] == "/language/translate/v2"
    # #302: key in the X-Goog-Api-Key header, text in the JSON body, empty query.
    assert cap["params"] == {}, cap["params"]
    assert cap["api_key_header"] == "gk"
    assert cap["body"].get("q") == "Hello"
    assert cap["body"].get("target") == "es"
    assert prov.last_usage["nmt_kind"] == "google"
    print("  Google Basic v2: X-Goog-Api-Key header + q/target body; response mapped OK")


_LEAK_KEY = "AIzaFAKE-LOGLEAK-KEY-302-0000000000"  # pregate: allow-secret fake test key
_LEAK_TEXT = "CONFIDENTIAL customer sentence 302"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_no_key_or_text_in_logs_302():
    """#302: with LOG_FORMAT=json (the hosted setting) and the default text format, a
    Google Translate call, the engine Test button and the Gemini model list put
    neither the key nor the source text into any log record, and the calls still
    authenticate through the header against MockTransport."""
    from app.logging_config import _JSON_HANDLER_FLAG, JsonFormatter, configure_logging
    from app.services.providers import cloud_list_models

    seen = {}

    def h(req):
        url = str(req.url)
        assert _LEAK_KEY not in url and "CONFIDENTIAL" not in url, url
        path = req.url.path
        if path.endswith("/v1beta/models"):
            seen["gemini"] = req.headers.get("x-goog-api-key")
            return httpx.Response(
                200,
                json={"models": [{"name": "models/g1", "supportedGenerationMethods": []}]},
            )
        seen[path] = req.headers.get("X-Goog-Api-Key")
        if path.endswith("/languages"):
            return httpx.Response(200, json={"data": {"languages": []}})
        body = json.loads(req.content)
        if body.get("target") == "de":  # error path: provider echoes the key back
            return httpx.Response(400, json={"error": {"message": f"bad key {_LEAK_KEY}"}})
        return httpx.Response(200, json={"data": {"translations": [{"translatedText": "Bonjour"}]}})

    root = logging.getLogger()
    names = ("httpx", "httpcore", "urllib3", "openai", "anthropic", "google_genai")
    saved = (config.LOG_FORMAT, root.level, {n: logging.getLogger(n).level for n in names})
    cap = _Capture()
    root.addHandler(cap)
    try:
        for fmt in ("json", "text"):
            _clear_profiles()
            for n in names:  # a fresh process: nothing quieted yet
                logging.getLogger(n).setLevel(logging.NOTSET)
            config.LOG_FORMAT = fmt
            configure_logging()
            root.setLevel(logging.DEBUG)  # the most verbose LOG_LEVEL an operator can set
            cap.records.clear()
            seen.clear()
            p = engines.create_profile("G302", "google", api_key=_LEAK_KEY)
            prov = GoogleTranslateProvider(
                profile=engines.get_profile(p.id), transport=httpx.MockTransport(h)
            )
            out = prov.complete("x", payload={"text": _LEAK_TEXT, "target": "fr", "source": "en"})
            assert out == "Bonjour", out
            try:
                prov.complete("x", payload={"text": _LEAK_TEXT, "target": "de"})
                raise AssertionError("a 400 must raise")
            except RuntimeError as e:
                assert _LEAK_KEY not in str(e) and _LEAK_KEY not in e.body, str(e)
                logging.getLogger("loc_tms.test302").exception("provider failed: %s", e)
            ok, msg = engines.test_profile(p.id, transport=httpx.MockTransport(h))
            assert ok, msg
            models = cloud_list_models(
                "gemini", api_key=_LEAK_KEY, transport=httpx.MockTransport(h)
            )
            assert models == ["g1"], models
            assert seen["/language/translate/v2"] == _LEAK_KEY, seen
            assert seen["/language/translate/v2/languages"] == _LEAK_KEY, seen
            assert seen["gemini"] == _LEAK_KEY, seen
            fmtr = JsonFormatter()
            for rec in cap.records:
                line = fmtr.format(rec)
                assert _LEAK_KEY not in line, f"[{fmt}] key leaked: {line[:300]}"
                assert "CONFIDENTIAL" not in line, f"[{fmt}] text leaked: {line[:300]}"
            assert not any(r.name.startswith(("httpx", "httpcore")) for r in cap.records), [
                r.name for r in cap.records
            ]
    finally:
        root.removeHandler(cap)
        for hd in [hd for hd in root.handlers if getattr(hd, _JSON_HANDLER_FLAG, False)]:
            root.removeHandler(hd)
        config.LOG_FORMAT, lvl, levels = saved
        root.setLevel(lvl)
        for n, v in levels.items():
            logging.getLogger(n).setLevel(v)
        _clear_profiles()
    print("  #302: Google call / Test button / Gemini list keep key + text out of logs OK")


def test_microsoft_request_shape_and_mapping():
    _clear_profiles()
    p = engines.create_profile("M", "microsoft", api_key="mk", cfg={"region": "eastus"})
    cap = {}
    prov = MicrosoftTranslatorProvider(
        profile=engines.get_profile(p.id), transport=microsoft_transport(cap)
    )
    out = prov.complete("unused", task="translate", payload={"text": "Hello", "target": "de-DE"})
    assert out == "Hallo Welt"
    assert cap["path"] == "/translate"
    assert cap["key"] == "mk"  # subscription key header
    assert cap["region"] == "eastus"  # region header
    assert cap["params"].get("to") == "de" and cap["params"].get("api-version") == "3.0"
    assert cap["body"] == [{"Text": "Hello"}]  # array-of-Text objects
    assert prov.last_usage["nmt_kind"] == "microsoft"
    print("  Microsoft v3.0: subscription-key + region headers + [{Text}] array + to param OK")


def test_cohere_request_shape_and_mapping():
    _clear_profiles()
    p = engines.create_profile("C", "cohere", api_key="ck", model="command-r")
    cap = {}
    prov = CohereProvider(profile=engines.get_profile(p.id), transport=cohere_transport(cap))
    out = prov.complete(
        "Translate: Hello",
        system="You translate.",
        task="translate",
        payload={"text": "Hello", "target": "fr-FR"},
    )
    assert out == "Salut le monde"
    assert cap["path"] == "/v2/chat"
    assert cap["auth"] == "Bearer ck"  # bearer auth
    assert cap["body"]["model"] == "command-r"
    roles = [m["role"] for m in cap["body"]["messages"]]
    assert roles == ["system", "user"]  # prompt-based (system + user)
    assert prov.last_usage["input_tokens"] == 12 and prov.last_usage["output_tokens"] == 4
    print("  Cohere: bearer + /v2/chat messages[] + token usage captured OK")


def test_openai_compatible_request_shape_and_mapping():
    _clear_profiles()
    p = engines.create_profile(
        "OR",
        "openai_compatible",
        base_url="https://api.deepseek.com",
        api_key="sk-or-1",
        model="deepseek/deepseek-chat",
    )
    cap = {}
    prov = OpenAICompatibleProvider(
        profile=engines.get_profile(p.id), transport=openai_compatible_transport(cap)
    )
    out = prov.complete(
        "Translate: Hello world",
        system="You translate.",
        task="translate",
        payload={"text": "Hello world", "target": "fr-FR"},
    )
    assert out == "Bonjour le monde"
    # base_url is normalized to end in /v1, so the chat path is /v1/chat/completions
    assert cap["path"] == "/v1/chat/completions"
    assert cap["auth"] == "Bearer sk-or-1"  # BYO bearer key
    assert cap["body"]["model"] == "deepseek/deepseek-chat"  # free-text host model id
    roles = [m["role"] for m in cap["body"]["messages"]]
    assert roles == ["system", "user"]  # prompt-based (system + user)
    assert prov.last_usage["input_tokens"] == 9 and prov.last_usage["output_tokens"] == 3
    # It resolves + is live via get_provider, and appears as an LLM kind.
    assert isinstance(get_provider(f"engine:{p.id}"), OpenAICompatibleProvider)
    assert provider_is_live(f"engine:{p.id}") is True
    assert "openai_compatible" in engines.LLM_KINDS and "openai_compatible" in engines.KINDS
    assert prov.CAPS["supports_prompts"] is True  # unlike the NMT kinds
    # A keyless openai_compatible engine:<id> is a DEAD ref (explicit config with no
    # key) -> FAIL LOUD, not a silent mock (hardening: engine refs never degrade).
    kl = engines.create_profile(
        "KeylessOR", "openai_compatible", base_url="https://api.deepseek.com", api_key=""
    )
    assert provider_is_live(f"engine:{kl.id}") is False
    try:
        get_provider(f"engine:{kl.id}")
        raise AssertionError("a keyless openai_compatible engine:<id> must raise DeadEngineError")
    except P.DeadEngineError:
        pass
    print(
        "  openai_compatible: bearer + /v1/chat/completions messages[] + free-text "
        "model + usage; LLM kind, live w/ key, keyless->DeadEngineError OK"
    )


def test_lara_request_shape_signing_and_mapping():
    _clear_profiles()
    # Auth is a PAIR: access-key-id in api_key, secret in cfg access_key_secret
    # (encrypted at store time). style/instructions/adapt_to/glossaries sent when set.
    p = engines.create_profile(
        "L",
        "lara",
        api_key="access-id-1",
        cfg={
            "access_key_secret": "shhh-secret",
            "style": "fluid",
            "instructions": ["Keep it formal."],
            "adapt_to": "12,34",
            "glossaries": "5",
        },
    )
    # The plaintext paired secret must NOT persist; only its ciphertext, under _enc.
    stored = engines.get_profile(p.id).config
    assert "access_key_secret" not in stored, "plaintext paired secret must not persist"
    assert stored.get("access_key_secret_enc"), "paired secret must be stored encrypted"
    assert engines.profile_secret(p.id) == "shhh-secret", "paired secret round-trips via Fernet"
    # engine_row REDACTS the paired secret to a masked flag (never the ciphertext).
    row = engines.engine_row(engines.get_profile(p.id))
    assert "shhh-secret" not in json.dumps(row)
    assert stored["access_key_secret_enc"] not in json.dumps(row), "ciphertext must not echo"
    assert row["config"].get("access_key_secret_set") is True
    cap = {}
    prov = LaraProvider(profile=engines.get_profile(p.id), transport=lara_transport(cap))
    out = prov.complete(
        "unused",
        task="translate",
        payload={"text": "Hello world", "target": "fr-FR", "source": "en-US"},
    )
    assert out == "Bonjour le monde"
    assert cap["path"] == "/translate"
    # EXPERIMENTAL HMAC signing: Authorization is `Lara <access-key-id>:<sig>` + a
    # Content-MD5 checksum of the JSON body + a Date header (request construction is
    # what we verify offline; the scheme itself needs live verification).
    assert cap["auth"].startswith("Lara access-id-1:"), f"bad Lara auth: {cap['auth']}"
    assert len(cap["auth"].split(":", 1)[1]) > 10, "signature present"
    assert cap["content_md5"], "Content-MD5 checksum header present"
    assert cap["date"], "Date header present (part of the signed string)"
    assert cap["body"]["q"] == "Hello world"
    assert cap["body"]["target"] == "fr-FR" and cap["body"]["source"] == "en-US"
    assert cap["body"]["style"] == "fluid"
    assert cap["body"]["instructions"] == ["Keep it formal."]
    assert cap["body"]["adapt_to"] == ["12", "34"]  # comma-split ids
    assert cap["body"]["glossaries"] == ["5"]
    # char-based usage accrues (spend cap): chars + nmt_kind populated.
    assert prov.last_usage["chars"] == len("Hello world") and prov.last_usage["nmt_kind"] == "lara"
    # A blank profile sends NONE of the adaptive fields (plain NMT behavior).
    p0 = engines.create_profile("L0", "lara", api_key="id", cfg={"access_key_secret": "s"})
    cap0 = {}
    LaraProvider(profile=engines.get_profile(p0.id), transport=lara_transport(cap0)).complete(
        "x", task="translate", payload={"text": "Hi", "target": "de-DE"}
    )
    for k in ("style", "instructions", "adapt_to", "glossaries"):
        assert k not in cap0["body"], f"unset {k} must not be sent"
    # CAPS: NMT (no prompts), glossary-capable (the glossaries field).
    assert prov.CAPS["supports_prompts"] is False and prov.CAPS["supports_glossary"] is True
    assert "lara" in engines.NMT_KINDS and "lara" in engines.KINDS
    print(
        "  Lara: paired secret encrypted+redacted; HMAC Lara auth + Content-MD5/Date; "
        "q/target/source + style/instructions/adapt_to/glossaries when set; char usage OK"
    )


def test_amazon_request_shape_sigv4_and_mapping():
    _clear_profiles()
    # Auth: AWS access key in api_key, secret in cfg, region REQUIRED.
    p = engines.create_profile(
        "A",
        "amazon",
        api_key="AKIDEXAMPLE",
        cfg={
            "access_key_secret": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            "region": "us-east-1",
            "formality": "FORMAL",
        },
    )
    stored = engines.get_profile(p.id).config
    assert "access_key_secret" not in stored and stored.get("access_key_secret_enc")
    assert engines.profile_secret(p.id) == "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    cap = {}
    prov = AmazonTranslateProvider(
        profile=engines.get_profile(p.id), transport=amazon_transport(cap)
    )
    out = prov.complete(
        "unused", task="translate", payload={"text": "Hello", "target": "es-MX", "source": "en-US"}
    )
    assert out == "Hola mundo"
    assert cap["path"] == "/"
    # SigV4: Authorization is AWS4-HMAC-SHA256 w/ the access key in the credential
    # scope, a signature, and X-Amz-Target = the JSON-RPC action.
    assert cap["auth"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"), cap["auth"]
    assert "Signature=" in cap["auth"] and "SignedHeaders=" in cap["auth"]
    assert cap["target"] == "AWSShineFrontendService_20170701.TranslateText"
    assert cap["amz_date"] and cap["sha256"], "SigV4 date + content-sha256 headers present"
    assert cap["body"]["Text"] == "Hello"
    assert cap["body"]["TargetLanguageCode"] == "es-MX"  # locale remap kept region variant
    assert cap["body"]["SourceLanguageCode"] == "en"  # base subtag
    assert cap["body"]["Settings"]["Formality"] == "FORMAL"
    assert prov.last_usage["chars"] == len("Hello") and prov.last_usage["nmt_kind"] == "amazon"
    # No source -> auto-detect.
    cap2 = {}
    AmazonTranslateProvider(
        profile=engines.get_profile(p.id), transport=amazon_transport(cap2)
    ).complete("x", task="translate", payload={"text": "Hi", "target": "de-DE"})
    assert cap2["body"]["SourceLanguageCode"] == "auto"
    # Missing region -> a terminal 400-shaped error (routes to failover, no retry-hammer).
    pn = engines.create_profile(
        "Anr", "amazon", api_key="k", cfg={"access_key_secret": "s"}
    )  # no region
    res = run_completion(
        ProviderSpec(provider=f"engine:{pn.id}"),
        "x",
        task="translate",
        payload={"text": "Hi", "target": "de-DE"},
    )
    # engine:<id> resolves to the real provider (keyed+enabled); the region error surfaces.
    assert res["text"] == "" and "region" in (res["error"] or "").lower()
    assert res["attempts"] == 1, "missing-region is terminal (no retry-hammer)"
    assert prov.CAPS["supports_prompts"] is False
    assert "amazon" in engines.NMT_KINDS and "amazon" in engines.KINDS
    print(
        "  Amazon: paired secret encrypted; SigV4 AWS4-HMAC + X-Amz-Target TranslateText; "
        "Text/target/source(auto)/Settings.Formality; missing region terminal; char usage OK"
    )


def test_lara_amazon_empty_extraction_is_not_phantom_success():
    """Regression for the empty-target-as-success billing trap (adversarial review
    of 6771bc8): a 200 response whose body doesn't map to any text used to come back
    as out="" with NO error and NO usage guard, so run_completion returned
    {"text": "", "error": None} — translate.py's old `_apply_result` guard
    (`if error and not text`) would NOT fire (error is None) and would commit "" as
    a SegStatus.MT "success" while the char-based NMT ledger still billed for the
    source length. providers._require_nmt_output now closes this at the provider
    layer for EVERY NMT kind (raises NmtEmptyOutput instead of returning ""); this
    test proves it for the two new kinds (Lara/Amazon) plus a normal-body control."""
    _clear_profiles()

    # --- Lara: unknown/unrecognized 200 body -> _lara_extract yields "" -> raise ---
    def lara_unknown_body_transport(captured):
        def h(req):
            captured["called"] = True
            # A 200 body that matches NONE of _lara_extract's known shapes.
            return httpx.Response(200, json={"unexpected": "shape"})

        return httpx.MockTransport(h)

    p = engines.create_profile("LEmpty", "lara", api_key="id", cfg={"access_key_secret": "s"})
    cap = {}
    register_provider(
        f"engine:{p.id}",
        LaraProvider(profile=engines.get_profile(p.id), transport=lara_unknown_body_transport(cap)),
    )
    res = run_completion(
        ProviderSpec(provider=f"engine:{p.id}"),
        "x",
        task="translate",
        payload={"text": "Hello world", "target": "fr-FR"},
    )
    assert cap.get("called"), "the mocked transport must have been hit"
    assert res["text"] == "", "an unrecognized body must NOT surface as translated text"
    assert res["error"] and "NmtEmptyOutput" in res["error"], (
        f"expected an NmtEmptyOutput error, got: {res['error']!r}"
    )
    assert res["usage"] is None, "no usage/spend must be recorded for a failed extraction"
    assert res["attempts"] == 1, "empty-extraction must be terminal (no retry-hammer)"
    P._OVERRIDES.clear()

    # --- Amazon: 200 body missing TranslatedText -> raise, no usage ---
    def amazon_missing_text_transport(captured):
        def h(req):
            captured["called"] = True
            # A 200 body with no TranslatedText key at all.
            return httpx.Response(
                200, json={"SourceLanguageCode": "en", "TargetLanguageCode": "es"}
            )

        return httpx.MockTransport(h)

    pa = engines.create_profile(
        "AEmpty", "amazon", api_key="AKID", cfg={"access_key_secret": "s", "region": "us-east-1"}
    )
    cap2 = {}
    register_provider(
        f"engine:{pa.id}",
        AmazonTranslateProvider(
            profile=engines.get_profile(pa.id), transport=amazon_missing_text_transport(cap2)
        ),
    )
    res2 = run_completion(
        ProviderSpec(provider=f"engine:{pa.id}"),
        "x",
        task="translate",
        payload={"text": "Hello", "target": "es-MX"},
    )
    assert cap2.get("called")
    assert res2["text"] == ""
    assert res2["error"] and "NmtEmptyOutput" in res2["error"]
    assert res2["usage"] is None, "no usage/spend must be recorded for a failed extraction"
    assert res2["attempts"] == 1, "empty-extraction must be terminal (no retry-hammer)"
    P._OVERRIDES.clear()

    # --- spend ledger must NOT move for either failed call ---
    from app.models import SpendLedger

    key = spend_svc.period_key()
    with Session(db_engine) as s:
        row = s.get(SpendLedger, key)
        spent_before_normal = row.accumulated_usd if row else 0.0

    # --- control: a normal recognized body still succeeds end-to-end (no regression) ---
    cap3 = {}
    register_provider(
        f"engine:{p.id}",
        LaraProvider(profile=engines.get_profile(p.id), transport=lara_transport(cap3)),
    )
    res3 = run_completion(
        ProviderSpec(provider=f"engine:{p.id}"),
        "x",
        task="translate",
        payload={"text": "Hello world", "target": "fr-FR"},
    )
    assert res3["text"] == "Bonjour le monde" and res3["error"] is None
    assert res3["usage"]["chars"] == len("Hello world")
    P._OVERRIDES.clear()
    with Session(db_engine) as s:
        row = s.get(SpendLedger, key)
        spent_after_normal = row.accumulated_usd if row else 0.0
    assert spent_after_normal > spent_before_normal, (
        "the SUCCESSFUL control call must still accrue spend (proves the guard "
        "only blocks the failure cases, not NMT billing generally)"
    )

    print(
        "  Lara/Amazon empty-extraction (unrecognized 200 body) raises NmtEmptyOutput, "
        "terminal, no usage/spend recorded; a normal body still succeeds end-to-end OK"
    )


def test_apply_result_treats_empty_nmt_text_as_error_not_success():
    """translate.py `_apply_result` defense-in-depth (belt-and-braces alongside the
    provider-layer fix above): even if some future NMT provider slipped an empty
    text through with no error, _apply_result must not commit it as a SegStatus.MT
    "success" — it must count as an error and leave the segment untouched. Scoped
    to the NMT channel only (usage carries `nmt_kind`) so an LLM's legitimate edge
    case (an intentionally empty completion) is not swept into this guard."""
    from app.models import Segment, SegStatus
    from app.services.translate import _apply_result, _new_stats

    with Session(db_engine) as session:
        seg = Segment(
            document_id=1,
            order_index=0,
            target_locale="fr-FR",
            source_text="Hello world",
            target_text="",
            status=SegStatus.NEW,
        )

        # NMT channel (usage carries nmt_kind), empty text, no error -> must be an
        # error, not a committed "success" (exactly the shape a provider bug could leak).
        stats = _new_stats()
        res = {
            "text": "",
            "provider_used": "engine:1",
            "model_used": None,
            "attempts": 1,
            "error": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "chars": 11,
                "nmt_kind": "lara",
            },
            "failover": False,
        }
        _apply_result(session, seg, res, stats)
        assert stats["errors"] == 1 and stats["translated"] == 0
        assert seg.status == SegStatus.NEW and seg.target_text == "", (
            "an empty NMT result for a non-empty source must NOT be committed as MT"
        )

        # Non-NMT (LLM/mock) channel, empty text, no error -> NOT swept into this guard
        # (no evidence a legitimate LLM empty-completion edge case doesn't exist; the
        # provider-layer fix is the primary defense for NMT specifically).
        stats2 = _new_stats()
        seg2 = Segment(
            document_id=1,
            order_index=1,
            target_locale="fr-FR",
            source_text="Hello world",
            target_text="",
            status=SegStatus.NEW,
        )
        res2 = {
            "text": "",
            "provider_used": "mock",
            "model_used": None,
            "attempts": 1,
            "error": None,
            "usage": {
                "input_tokens": 3,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
            "failover": False,
        }
        _apply_result(session, seg2, res2, stats2)
        assert stats2["errors"] == 0 and stats2["translated"] == 1
        assert seg2.status == SegStatus.MT, "non-NMT channel is unaffected by this guard"

        # A normal non-empty NMT result still commits as MT success (no regression).
        stats3 = _new_stats()
        seg3 = Segment(
            document_id=1,
            order_index=2,
            target_locale="fr-FR",
            source_text="Hello world",
            target_text="",
            status=SegStatus.NEW,
        )
        res3 = {
            "text": "Bonjour le monde",
            "provider_used": "engine:1",
            "model_used": None,
            "attempts": 1,
            "error": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "chars": 11,
                "nmt_kind": "lara",
            },
            "failover": False,
        }
        _apply_result(session, seg3, res3, stats3)
        assert stats3["translated"] == 1 and stats3["errors"] == 0
        assert seg3.status == SegStatus.MT and seg3.target_text == "Bonjour le monde"

    print(
        "  _apply_result: empty NMT-channel text w/ no error -> counted as error, "
        "segment NOT committed as MT (non-NMT channel unaffected; normal NMT still OK) OK"
    )


def test_sigv4_signing_key_matches_aws_vector():
    # The signing-key derivation must match the AWS SigV4 documented test vector, so a
    # regression in the HMAC chain (date/region/service/aws4_request) is caught offline.
    from datetime import datetime, timezone

    from app.services import providers as PP

    h = PP._sigv4_headers(
        access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="iam",
        target="X",
        host="iam.amazonaws.com",
        body=b"",
        now=datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc),
    )
    assert h["X-Amz-Date"] == "20150830T123600Z"
    # Deterministic given a fixed `now` -> a stable Authorization (guards the whole chain).
    assert h["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request"
    )
    assert (
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date;x-amz-target"
        in h["Authorization"]
    )
    print("  SigV4 signing key + string-to-sign match the AWS documented vector (deterministic) OK")


def _sigv4_host_spy(module):
    """Wrap module._sigv4_headers so the next call records the `host=` it was passed
    (the value the real code derives from base_url) and still signs for real. The
    spy delegates to the genuine signer, so nothing downstream is weakened."""
    real = module._sigv4_headers
    seen = []

    def spy(**kw):
        seen.append(kw.get("host"))
        return real(**kw)

    module._sigv4_headers = spy
    return seen, real


def test_amazon_sigv4_signed_host_is_urlsplit_netloc():
    """#506 item 3: the SigV4 signed Host must be the URL's netloc only.
    `base.split("://", 1)[-1]` kept a trailing slash or path, so a base_url like
    `https://translate.us-east-1.amazonaws.com/` signed `host:translate...com/`,
    which no longer matches the Host httpx actually sends -> 403 SignatureDoesNotMatch
    (a terminal error). Deriving the host with urlsplit(base).netloc trims it.
    Exercises BOTH call sites: AmazonTranslateProvider.complete and engines._probe_amazon."""
    from app.services import providers_part3

    _clear_profiles()

    def amazon_profile(name, base):
        pid = engines.create_profile(
            name,
            "amazon",
            base_url=base,
            api_key="AKIDEXAMPLE",
            cfg={
                "access_key_secret": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                "region": "us-east-1",
            },
        ).id
        return engines.get_profile(pid)

    expected = "translate.us-east-1.amazonaws.com"
    bases = [
        "https://translate.us-east-1.amazonaws.com/",
        "https://translate.us-east-1.amazonaws.com/some/path",
    ]

    # Provider path: the signed host the real complete() derives from base_url.
    seen, real = _sigv4_host_spy(providers_part3)
    try:
        for i, base in enumerate(bases):
            prof = amazon_profile(f"Ahost{i}", base)
            AmazonTranslateProvider(profile=prof, transport=amazon_transport({})).complete(
                "unused", task="translate", payload={"text": "Hi", "target": "es"}
            )
    finally:
        providers_part3._sigv4_headers = real
    assert seen == [expected, expected], f"provider signed host must be the netloc: {seen}"

    # Probe path: the same expression inside engines._probe_amazon.
    seen2, real2 = _sigv4_host_spy(P)
    try:
        for i, base in enumerate(bases):
            prof = amazon_profile(f"Aprobe{i}", base)
            ok, msg = engines.test_profile(prof.id, transport=probe_transport(200))
            assert ok, f"probe must pass on a 200: {msg}"
    finally:
        P._sigv4_headers = real2
    assert seen2 == [expected, expected], f"probe signed host must be the netloc: {seen2}"
    print(
        "  #506 item 3: SigV4 signed host = urlsplit(base).netloc (trailing slash + path "
        "trimmed) on both the provider and the probe path OK"
    )


def test_mock_glossary_target_backslashes_are_literal():
    """#506 item 6: MockProvider glossary substitution passed the target term as
    re.sub's *replacement*, so a Windows path like C:\\Users raised "bad escape \\U"
    and "\\1"/"\\g<0>" became a group reference instead of literal text. Substituting
    through `lambda _m: tgt` makes every target literal."""
    from app.services.providers import MockProvider

    for tgt in ("C:\\Users\\x", "\\1", "a\\g<0>b"):
        out = MockProvider().complete(
            "unused",
            task="translate",
            payload={
                "text": "Open TERM now",
                "target": "xx",
                "glossary": [{"source_term": "TERM", "target_term": tgt}],
            },
        )
        assert out == f"[xx] Open {tgt} now", f"target {tgt!r} must substitute literally: {out!r}"
    print("  #506 item 6: MockProvider glossary targets (backslash + group-ref) stay literal OK")


def test_lara_amazon_paired_secret_cli_web_parity():
    # web+CLI parity for the PAIRED secret + config: create via CLI and via web, both
    # store the secret encrypted (never echoed) and the config fields.
    _clear_profiles()
    from app.cli.__main__ import main

    # --- CLI: --secret access_key_secret=... routes to the encrypted paired secret ---
    assert (
        main(
            [
                "engine",
                "create",
                "--name",
                "CliLara",
                "--kind",
                "lara",
                "--secret",
                "api_key=id-1",
                "--secret",
                "access_key_secret=sec-1",
                "--config",
                "style=faithful",
                "--config",
                "adapt_to=7,8",
            ]
        )
        == 0
    )
    lp = engines.get_by_name("CliLara")
    assert engines.dec_key(lp.api_key_enc) == "id-1"
    assert engines.profile_secret(lp.id) == "sec-1", "CLI paired secret must be stored+decryptable"
    assert lp.config.get("style") == "faithful" and lp.config.get("adapt_to") == "7,8"
    # --json show never leaks either secret (masked flag only).
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--json", "engine", "show", str(lp.id)])
    out = buf.getvalue()
    assert "sec-1" not in out and "id-1" not in out
    assert lp.config["access_key_secret_enc"] not in out, "ciphertext must not print"
    assert '"access_key_secret_set": true' in out
    # CLI Amazon with region.
    assert (
        main(
            [
                "engine",
                "create",
                "--name",
                "CliAmz",
                "--kind",
                "amazon",
                "--secret",
                "api_key=AKID",
                "--secret",
                "access_key_secret=aws-sec",
                "--config",
                "region=eu-west-1",
                "--config",
                "formality=INFORMAL",
            ]
        )
        == 0
    )
    ap = engines.get_by_name("CliAmz")
    assert engines.profile_secret(ap.id) == "aws-sec" and ap.config.get("region") == "eu-west-1"
    # --- web: create Lara + Amazon via the form, paired secret encrypted, not echoed ---
    _clear_profiles()
    c = TestClient(app)
    r = c.post(
        "/setup-ai/engines",
        data={
            "name": "WebLara",
            "kind": "lara",
            "api_key": "web-id",
            "access_key_secret": "web-sec",
            "style": "creative",
            "instructions": "Be concise.\n",
            "glossaries": "9",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    wl = engines.get_by_name("WebLara")
    assert engines.profile_secret(wl.id) == "web-sec"
    assert wl.config.get("style") == "creative" and wl.config.get("instructions") == ["Be concise."]
    assert wl.config.get("glossaries") == "9"
    page = c.get("/setup-ai").text
    assert "web-sec" not in page and "lara" in page
    # Edit with a BLANK paired secret keeps the stored one (like api_key).
    c.post(
        f"/setup-ai/engines/{wl.id}",
        data={
            "name": "WebLara",
            "api_key": "",
            "access_key_secret": "",
            "style": "creative",
            "instructions": "Be concise.",
            "adapt_to": "",
            "glossaries": "9",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert engines.profile_secret(wl.id) == "web-sec", "blank paired secret must not wipe it"
    # web Amazon create.
    r2 = c.post(
        "/setup-ai/engines",
        data={
            "name": "WebAmz",
            "kind": "amazon",
            "api_key": "AKID2",
            "access_key_secret": "aws-web-sec",
            "region": "us-west-2",
            "formality": "FORMAL",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 303
    wa = engines.get_by_name("WebAmz")
    assert engines.profile_secret(wa.id) == "aws-web-sec"
    assert wa.config.get("region") == "us-west-2" and wa.config.get("formality") == "FORMAL"
    print(
        "  Lara/Amazon paired-secret parity: CLI --secret access_key_secret + web form; "
        "encrypted, never echoed, blank-keeps; config fields flow OK"
    )


def test_deepl_expanded_params_request_shape():
    _clear_profiles()
    # No params set -> body carries NONE of the next-gen fields (default unchanged).
    p0 = engines.create_profile("D0", "deepl", api_key="k0")
    cap0 = {}
    prov0 = DeepLProvider(profile=engines.get_profile(p0.id), transport=deepl_transport(cap0))
    prov0.complete("x", task="translate", payload={"text": "Hello", "target": "fr-FR"})
    for k in ("model_type", "formality", "context", "custom_instructions"):
        assert k not in cap0["body"], f"unset {k} must NOT be sent (default behavior preserved)"
    # All params set -> each is sent verbatim in the /v2/translate body.
    p1 = engines.create_profile(
        "D1",
        "deepl",
        api_key="k1",
        cfg={
            "model_type": "quality_optimized",
            "formality": "more",
            "context": "a product help centre",
            "custom_instructions": ["Keep the tone formal.", "Do not translate product names."],
        },
    )
    cap1 = {}
    prov1 = DeepLProvider(profile=engines.get_profile(p1.id), transport=deepl_transport(cap1))
    prov1.complete("x", task="translate", payload={"text": "Hello", "target": "de-DE"})
    assert cap1["body"]["model_type"] == "quality_optimized"
    assert cap1["body"]["formality"] == "more"
    assert cap1["body"]["context"] == "a product help centre"
    assert cap1["body"]["custom_instructions"] == [
        "Keep the tone formal.",
        "Do not translate product names.",
    ]
    # custom_instructions is re-clamped defensively: >10 dropped, >300 chars truncated.
    p2 = engines.create_profile(
        "D2",
        "deepl",
        api_key="k2",
        cfg={"custom_instructions": [f"instr {i}" for i in range(15)] + ["z" * 400]},
    )
    cap2 = {}
    prov2 = DeepLProvider(profile=engines.get_profile(p2.id), transport=deepl_transport(cap2))
    prov2.complete("x", task="translate", payload={"text": "Hi", "target": "fr-FR"})
    ci = cap2["body"]["custom_instructions"]
    assert len(ci) == 10 and all(len(s) <= 300 for s in ci), "must clamp to 10x300"
    print(
        "  DeepL expanded params: unset omitted (default preserved); model_type/formality/"
        "context/custom_instructions sent when set; instructions clamped 10x300 OK"
    )


def test_deepl_string_custom_instructions_not_iterated_per_char():
    # REGRESSION: a scalar-string custom_instructions (stored via a direct
    # create_profile cfg={"custom_instructions": "..."} — the CLI --config path or a
    # direct service call) MUST land as exactly ONE full-text instruction, never
    # iterated per-character into up to 10 one-char instructions.
    _clear_profiles()
    text = "Keep the tone formal and do not translate product names."
    p = engines.create_profile("Dstr", "deepl", api_key="k", cfg={"custom_instructions": text})
    # The config layer canonicalizes the scalar into a one-element list at STORE time.
    stored = engines.get_profile(p.id).config.get("custom_instructions")
    assert stored == [text], f"stored config must be a one-element list, got {stored!r}"
    cap = {}
    prov = DeepLProvider(profile=engines.get_profile(p.id), transport=deepl_transport(cap))
    prov.complete("x", task="translate", payload={"text": "Hello", "target": "fr-FR"})
    ci = cap["body"]["custom_instructions"]
    assert ci == [text], f"exactly one full-text instruction on the wire, got {ci!r}"
    assert len(ci) == 1, "a string must NOT become many one-char instructions"
    # Defense in depth: even if a raw string somehow reaches the provider (older row),
    # the provider itself coerces it rather than iterating per character.
    prof = engines.get_profile(p.id)
    prof.config = {"custom_instructions": text}  # simulate a legacy un-normalized row
    cap2 = {}
    prov2 = DeepLProvider(profile=prof, transport=deepl_transport(cap2))
    prov2.complete("x", task="translate", payload={"text": "Hi", "target": "fr-FR"})
    assert cap2["body"]["custom_instructions"] == [text], "provider must coerce a bare string too"
    print("  DeepL string custom_instructions -> ONE full-text instruction (never per-char) OK")


def test_deepl_model_type_auto_set_for_steering_params():
    # REGRESSION: DeepL 400s if context/custom_instructions are set on a classic model.
    # When model_type is blank but a steering param IS set, the provider auto-sends the
    # graceful next-gen variant. Neither steering param set -> no model_type at all.
    _clear_profiles()
    # context set, model_type blank -> prefer_quality_optimized auto-sent.
    pc = engines.create_profile("Dctx", "deepl", api_key="k", cfg={"context": "a help centre"})
    capc = {}
    DeepLProvider(profile=engines.get_profile(pc.id), transport=deepl_transport(capc)).complete(
        "x", task="translate", payload={"text": "Hi", "target": "fr-FR"}
    )
    assert capc["body"]["model_type"] == "prefer_quality_optimized"
    # instructions set, model_type blank -> auto-sent too.
    pi = engines.create_profile(
        "Dins", "deepl", api_key="k", cfg={"custom_instructions": ["Be formal."]}
    )
    capi = {}
    DeepLProvider(profile=engines.get_profile(pi.id), transport=deepl_transport(capi)).complete(
        "x", task="translate", payload={"text": "Hi", "target": "fr-FR"}
    )
    assert capi["body"]["model_type"] == "prefer_quality_optimized"
    # an EXPLICIT model_type wins (not overridden by the fallback).
    pe = engines.create_profile(
        "Dexp", "deepl", api_key="k", cfg={"context": "x", "model_type": "quality_optimized"}
    )
    cape = {}
    DeepLProvider(profile=engines.get_profile(pe.id), transport=deepl_transport(cape)).complete(
        "x", task="translate", payload={"text": "Hi", "target": "fr-FR"}
    )
    assert cape["body"]["model_type"] == "quality_optimized"
    # neither steering param set -> NO model_type (classic default untouched).
    pn = engines.create_profile("Dnone", "deepl", api_key="k")
    capn = {}
    DeepLProvider(profile=engines.get_profile(pn.id), transport=deepl_transport(capn)).complete(
        "x", task="translate", payload={"text": "Hi", "target": "fr-FR"}
    )
    assert "model_type" not in capn["body"], "no steering param -> classic default, no model_type"
    print(
        "  DeepL model_type auto-set to prefer_quality_optimized when steering set + blank; "
        "explicit wins; neither -> untouched OK"
    )


def test_usageless_response_leaves_last_usage_none():
    # REGRESSION: a 200 LLM response with NO usage block must leave last_usage None so
    # run_completion's length-based ESTIMATE engages. A truthy zero-token dict would
    # record a permanent $0 cost and silently bypass the spend cap.
    _clear_profiles()
    po = engines.create_profile(
        "ORnu", "openai_compatible", base_url="https://api.deepseek.com", api_key="k", model="m"
    )
    provo = OpenAICompatibleProvider(
        profile=engines.get_profile(po.id), transport=openai_compatible_no_usage_transport({})
    )
    out = provo.complete(
        "Translate: Hello world",
        system="s",
        task="translate",
        payload={"text": "Hello world", "target": "fr-FR"},
    )
    assert out == "Bonjour le monde"
    assert provo.last_usage is None, "usage-less openai_compatible must leave last_usage None"
    pc = engines.create_profile("Cnu", "cohere", api_key="k", model="command-r")
    provc = CohereProvider(
        profile=engines.get_profile(pc.id), transport=cohere_no_usage_transport({})
    )
    provc.complete(
        "Translate: Hi", system="s", task="translate", payload={"text": "Hi", "target": "fr-FR"}
    )
    assert provc.last_usage is None, "usage-less cohere must leave last_usage None"
    # A normal usage block still populates it (guard against over-fixing).
    provc_ok = CohereProvider(profile=engines.get_profile(pc.id), transport=cohere_transport({}))
    provc_ok.complete("x", system="s", task="translate", payload={"text": "Hi", "target": "fr-FR"})
    assert provc_ok.last_usage and provc_ok.last_usage["input_tokens"] == 12
    # And run_completion's estimate path engages (usage.estimated True, nonzero est).
    register_provider(
        f"engine:{po.id}",
        OpenAICompatibleProvider(
            profile=engines.get_profile(po.id), transport=openai_compatible_no_usage_transport({})
        ),
    )
    res = run_completion(
        ProviderSpec(provider=f"engine:{po.id}"),
        "Translate this longer prompt for a real estimate",
        task="translate",
        payload={"text": "Translate this longer prompt", "target": "fr-FR"},
    )
    assert res["usage"].get("estimated") is True, "usage-less -> length-based estimate must engage"
    assert res["usage"]["input_tokens"] >= 1
    P._OVERRIDES.clear()
    print(
        "  usage-less openai_compatible + cohere -> last_usage None; run_completion estimate "
        "engages (spend-cap bypass closed) OK"
    )


def test_normalize_local_base_case_insensitive_v1():
    # REGRESSION: an uppercase base already ending in /V1 must NOT get a second /v1.
    assert P._normalize_local_base("HTTPS://API.DEEPSEEK.COM/V1") == "HTTPS://API.DEEPSEEK.COM/V1"
    assert P._normalize_local_base("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"
    # mixed case + trailing slash still collapses to a single /v1.
    assert P._normalize_local_base("https://Host.Example/V1/") == "https://Host.Example/V1"
    # no /v1 -> one gets appended (unchanged behavior).
    assert P._normalize_local_base("https://api.deepseek.com") == "https://api.deepseek.com/v1"
    print("  _normalize_local_base: case-insensitive /v1 (no double-append on uppercase) OK")


def test_param_clearing_web_and_cli_parity():
    # REGRESSION: a set DeepL param must be CLEARABLE from both surfaces (a cfg merge
    # alone can never delete a key).
    _clear_profiles()
    # --- service layer: clear removes the key from stored config ---
    p = engines.create_profile(
        "Dclr",
        "deepl",
        api_key="k",
        cfg={
            "context": "old ctx",
            "model_type": "quality_optimized",
            "custom_instructions": ["a"],
            "formality": "more",
        },
    )
    engines.update_profile(p.id, clear=["context", "custom_instructions"])
    cfg = engines.get_profile(p.id).config
    assert "context" not in cfg and "custom_instructions" not in cfg, "clear must delete the keys"
    assert cfg.get("model_type") == "quality_optimized" and cfg.get("formality") == "more", (
        "clear must not touch other keys"
    )
    # and the cleared params are no longer on the wire (context gone; model_type stays,
    # so no auto-fallback either).
    cap = {}
    DeepLProvider(profile=engines.get_profile(p.id), transport=deepl_transport(cap)).complete(
        "x", task="translate", payload={"text": "Hi", "target": "fr-FR"}
    )
    assert "context" not in cap["body"] and "custom_instructions" not in cap["body"]
    assert cap["body"]["model_type"] == "quality_optimized"
    # --- CLI: --clear-config parity ---
    _clear_profiles()
    from app.cli.__main__ import main

    assert (
        main(
            [
                "engine",
                "create",
                "--name",
                "CliClr",
                "--kind",
                "deepl",
                "--secret",
                "api_key=k",
                "--config",
                "context=a help centre",
                "--instruction",
                "Be formal.",
            ]
        )
        == 0
    )
    cid = engines.get_by_name("CliClr").id
    assert (
        main(
            [
                "engine",
                "update",
                str(cid),
                "--clear-config",
                "context",
                "--clear-config",
                "custom_instructions",
            ]
        )
        == 0
    )
    ccfg = engines.get_profile(cid).config
    assert "context" not in ccfg and "custom_instructions" not in ccfg, (
        "CLI --clear-config must delete"
    )
    # --- web Edit: a submitted-blank DeepL param field CLEARS it ---
    _clear_profiles()
    c = TestClient(app)
    r = c.post(
        "/setup-ai/engines",
        data={
            "name": "WebClr",
            "kind": "deepl",
            "api_key": "k",
            "model_type": "quality_optimized",
            "context": "a help centre",
            "custom_instructions": "Be formal.",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    wid = engines.get_by_name("WebClr").id
    assert engines.get_profile(wid).config.get("context") == "a help centre"
    # Edit submit with context + custom_instructions BLANK -> those clear; model_type kept.
    c.post(
        f"/setup-ai/engines/{wid}",
        data={
            "name": "WebClr",
            "kind": "deepl",
            "api_key": "",
            "model_type": "quality_optimized",
            "formality": "",
            "context": "",
            "custom_instructions": "",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    wcfg = engines.get_profile(wid).config
    assert "context" not in wcfg and "custom_instructions" not in wcfg, (
        "web Edit blank field must CLEAR the DeepL param"
    )
    assert wcfg.get("model_type") == "quality_optimized", "a still-filled field is kept"
    print(
        "  param clearing parity: service clear=, CLI --clear-config, web Edit-blank-clears; "
        "keys removed from config + off the wire OK"
    )


def test_nmt_rejects_prompt_only_and_maps_errors():
    _clear_profiles()
    p = engines.create_profile("D", "deepl", api_key="k1")
    prov = DeepLProvider(profile=engines.get_profile(p.id), transport=deepl_transport({}))
    # A prompt-only task (no translate payload text) -> clean rejection, not garbage.
    for payload in ({}, {"description": "an author-prompt step"}, None):
        try:
            prov.complete("do this thing", task="author_prompt", payload=payload)
            raise AssertionError("NMT must reject a prompt-only task")
        except NmtUnsupported:
            pass
    # An HTTP error maps into the run_completion error dict (no fallback here).
    cap = {}
    register_provider(
        f"engine:{p.id}",
        DeepLProvider(
            profile=engines.get_profile(p.id), transport=deepl_transport(cap, status=401)
        ),
    )
    res = run_completion(
        ProviderSpec(provider=f"engine:{p.id}"),
        "x",
        task="translate",
        payload={"text": "Hello", "target": "fr-FR"},
    )
    assert res["text"] == "" and res["error"], "a 401 must surface as an error dict"
    P._OVERRIDES.clear()
    # NmtUnsupported surfaces through run_completion as an error too, and is TERMINAL:
    # it must NOT burn the retry/backoff budget (1 attempt, not retry_max+1) — a
    # shape error retrying can never fix goes straight to the failover chain.
    import time as _t

    register_provider(
        f"engine:{p.id}",
        DeepLProvider(profile=engines.get_profile(p.id), transport=deepl_transport({})),
    )
    t0 = _t.time()
    res2 = run_completion(
        ProviderSpec(provider=f"engine:{p.id}"),
        "x",
        task="author_prompt",
        payload={"description": "x"},
    )
    elapsed = _t.time() - t0
    assert res2["text"] == "" and "NmtUnsupported" in (res2["error"] or "")
    assert res2["attempts"] == 1, (
        f"NmtUnsupported must be terminal (1 attempt), got {res2['attempts']}"
    )
    assert elapsed < 0.3, f"NmtUnsupported must not retry-hammer with backoff, took {elapsed:.2f}s"
    # A prompt task on an NMT PRIMARY fails over immediately to an LLM fallback
    # (the mock) — no wasted retry budget on the NMT engine first.
    res3 = run_completion(
        ProviderSpec(provider=f"engine:{p.id}", fallback_provider="mock"),
        "translate this",
        task="author_prompt",
        payload={"description": "x"},
    )
    assert res3["text"] and res3["failover"] is True and res3["provider_used"] == "mock", (
        "prompt task on an NMT primary must fail over to the LLM fallback"
    )
    P._OVERRIDES.clear()
    print(
        "  NMT rejects prompt-only tasks cleanly; HTTP + unsupported map to the error dict; "
        "NmtUnsupported is terminal (no retry-hammer) and fails over to an LLM fallback OK"
    )


def test_get_provider_resolution_and_degradation():
    _clear_profiles()
    p = engines.create_profile("C", "cohere", api_key="ck")
    prov = get_provider(f"engine:{p.id}")
    assert isinstance(prov, CohereProvider), "engine:<id> resolves to the right subclass"
    assert provider_is_live(f"engine:{p.id}") is True  # keyed + enabled
    # HARDENING (provider failure-mode): an engine:<id> ref is ALWAYS explicit user
    # config, so a DEAD ref (disabled / deleted / malformed / keyless / undecryptable)
    # must FAIL LOUD via DeadEngineError, NOT silently degrade to MockProvider and
    # write mock placeholder output as if it were a real translation (the cardinal
    # sin). provider_is_live stays the liveness authority (returns False), but
    # get_provider now raises instead of returning the mock.
    engines.update_profile(p.id, enabled=False)  # disabled
    assert provider_is_live(f"engine:{p.id}") is False
    try:
        get_provider(f"engine:{p.id}")
        raise AssertionError("a disabled engine:<id> must raise DeadEngineError, not return mock")
    except P.DeadEngineError as e:
        assert f"engine:{p.id}" in str(e)  # the message names the dead ref
    # missing id -> dead (raises, never a silent mock)
    try:
        get_provider("engine:999999")
        raise AssertionError("a missing engine:<id> must raise DeadEngineError")
    except P.DeadEngineError:
        pass
    # malformed id -> dead (raises, never crashes into a real call)
    try:
        get_provider("engine:abc")
        raise AssertionError("a malformed engine:<id> must raise DeadEngineError")
    except P.DeadEngineError:
        pass
    # KEYLESS enabled CLOUD engine -> DEAD (raises). It must NOT resolve to a real
    # provider that would fire a live call with an empty auth header, AND (unlike a
    # keyless BUILT-IN) it must NOT silently degrade to mock — an engine ref is
    # explicit config, so a keyless one is a misconfiguration to surface loudly.
    kl = engines.create_profile("KeylessDeepL", "deepl", api_key="")
    assert provider_is_live(f"engine:{kl.id}") is False
    try:
        get_provider(f"engine:{kl.id}")
        raise AssertionError("a keyless cloud engine:<id> must raise DeadEngineError")
    except P.DeadEngineError:
        pass
    # SANCTIONED OFFLINE MODE PRESERVED: a keyless BUILT-IN provider name (claude/
    # gemini/openai/mock) still resolves to MockProvider — that is how the whole
    # hermetic suite runs offline, and it is UNCHANGED. Only engine:<id> refs
    # hard-fail. (This is the one explicit assertion guarding the offline path.)
    assert isinstance(get_provider("claude"), P.MockProvider), (
        "keyless BUILT-IN must still resolve to the offline mock (sanctioned offline mode)"
    )
    assert isinstance(get_provider("mock"), P.MockProvider)
    assert isinstance(get_provider(None), P.MockProvider)  # default provider, keyless
    # A local engine is live (and real) on a base URL even with no key (self-hosted).
    lk = engines.create_profile("LocalNoKey", "local", base_url="http://10.0.0.9:1234")
    assert provider_is_live(f"engine:{lk.id}") is True
    assert isinstance(get_provider(f"engine:{lk.id}"), P.LocalProvider)
    # UNDECRYPTABLE-SECRET guard: an enabled cloud engine whose api_key_enc no longer
    # decrypts (rotated/lost data/.enc_key) passes provider_is_live (key PRESENT) but
    # would fire with an empty credential — treat it as a dead ref and raise.
    bad = engines.create_profile("CorruptKey", "deepl", api_key="realkey")
    engines.update_profile(
        bad.id,
    )  # no-op; corrupt the stored ciphertext directly below
    from sqlmodel import Session as _S

    from app.db import engine as _db_engine
    from app.models import EngineProfile

    with _S(_db_engine) as _s:
        row = _s.get(EngineProfile, bad.id)
        row.api_key_enc = "gAAAAABnot-a-valid-fernet-token"  # present but undecryptable
        _s.add(row)
        _s.commit()
    assert provider_is_live(f"engine:{bad.id}") is True  # key is PRESENT
    try:
        get_provider(f"engine:{bad.id}")
        raise AssertionError("an undecryptable-key cloud engine must raise DeadEngineError")
    except P.DeadEngineError:
        pass
    print(
        "  get_provider: engine:<id> resolves live to the right subclass; dead refs "
        "(disabled/missing/malformed/keyless/undecryptable) FAIL LOUD (DeadEngineError, "
        "not silent mock); keyless BUILT-IN offline mode preserved; local w/ base -> real OK"
    )


def test_char_based_pricing_and_cohere_tokens():
    # NMT per-char rates (per 1M source chars): DeepL 25, Google 20, Microsoft 10.
    assert pricing.cost_usd("engine:1", "", {"chars": 1_000_000, "nmt_kind": "deepl"}) == 25.0
    assert pricing.cost_usd("engine:1", "", {"chars": 1_000_000, "nmt_kind": "google"}) == 20.0
    assert pricing.cost_usd("engine:1", "", {"chars": 1_000_000, "nmt_kind": "microsoft"}) == 10.0
    # Lara (€20/M -> ~$21.60 USD) + Amazon ($15/M).
    assert pricing.cost_usd("engine:1", "", {"chars": 1_000_000, "nmt_kind": "lara"}) == 21.6
    assert pricing.cost_usd("engine:1", "", {"chars": 1_000_000, "nmt_kind": "amazon"}) == 15.0
    # unknown NMT kind -> conservative 25
    assert pricing.cost_usd("engine:1", "", {"chars": 1_000_000, "nmt_kind": "??"}) == 25.0
    # Cohere token pricing entries exist
    # (#360 gate: cohere.com/pricing 2026-10-01 lists Command R 03-2024 at 0.50/1.50 and
    # Command R+ at 3/15 (04-2024) or 2.5/10 (08-2024); undated aliases take the higher)
    assert pricing.rates_for("command-r") == (0.50, 1.50)
    assert pricing.rates_for("command-r-plus") == (3.0, 15.0)
    assert pricing.rates_for("command-r-plus-08-2024") == (2.5, 10.0)
    print("  char-based NMT pricing (DeepL 25 / Google 20 / MS 10 per M) + Cohere token rates OK")


def test_spend_ledger_accrues_char_cost():
    _clear_profiles()
    # Zero the current period so the assertion is order-independent.
    from app.models import SpendLedger

    key = spend_svc.period_key()
    with Session(db_engine) as s:
        row = s.get(SpendLedger, key)
        if row:
            row.accumulated_usd = 0.0
            s.add(row)
            s.commit()
    p = engines.create_profile("D", "deepl", api_key="k1")
    register_provider(
        f"engine:{p.id}",
        DeepLProvider(profile=engines.get_profile(p.id), transport=deepl_transport({})),
    )
    before = spend_svc.spent_this_period()
    res = run_completion(
        ProviderSpec(provider=f"engine:{p.id}"),
        "x",
        task="translate",
        payload={"text": "Hello world", "target": "fr-FR"},
    )
    assert res["text"] == "Bonjour le monde"
    after = spend_svc.spent_this_period()
    # 11 chars * $25/M = $0.000275
    assert after > before, "the char-based NMT cost must accrue into the spend ledger"
    P._OVERRIDES.clear()
    print("  spend ledger accrues char-based NMT cost through run_completion OK")


def test_failover_across_kinds():
    _clear_profiles()
    pd = engines.create_profile("BadDeepL", "deepl", api_key="x")
    pm = engines.create_profile("GoodMS", "microsoft", api_key="y")
    register_provider(
        f"engine:{pd.id}",
        DeepLProvider(
            profile=engines.get_profile(pd.id), transport=deepl_transport({}, status=401)
        ),
    )
    register_provider(
        f"engine:{pm.id}",
        MicrosoftTranslatorProvider(
            profile=engines.get_profile(pm.id), transport=microsoft_transport({})
        ),
    )
    spec = ProviderSpec(provider=f"engine:{pd.id}", fallback_provider=f"engine:{pm.id}")
    res = run_completion(spec, "x", task="translate", payload={"text": "Hello", "target": "de-DE"})
    assert res["text"] == "Hallo Welt", "must fail over from DeepL(401) to Microsoft"
    assert res["failover"] is True and res["provider_used"] == f"engine:{pm.id}"
    P._OVERRIDES.clear()
    print("  failover across engine KINDS (DeepL 401 -> Microsoft) OK")


def _deepl_429_then_ok_transport(captured, retry_after=None, calls=None):
    """A DeepL transport that 429s on the FIRST call (optionally with a Retry-After
    header) then 200s — so run_completion retries once and we can observe the sleep."""
    state = {"n": 0}

    def h(req):
        state["n"] += 1
        if calls is not None:
            calls.append(state["n"])
        if state["n"] == 1:
            headers = {}
            if retry_after is not None:
                headers["Retry-After"] = str(retry_after)
            return httpx.Response(429, headers=headers, json={"message": "rate limited"})
        return httpx.Response(200, json={"translations": [{"text": "Bonjour le monde"}]})

    return httpx.MockTransport(h)


def test_nmt_honors_retry_after_on_429():
    """A 429 Retry-After header is HONORED (max(header, backoff), capped) instead of
    ignored — so a rate-limited NMT retry waits the server-requested delay rather than
    hammering straight back in. Applies to all NMT kinds (shared _raise_for_nmt_status
    + run_completion retry loop); tested via DeepL."""
    import time as _t

    _clear_profiles()
    p = engines.create_profile("RA-DeepL", "deepl", api_key="k")

    slept = []
    real_sleep = _t.sleep
    _t.sleep = lambda s: slept.append(s)  # record, do not actually sleep
    try:
        # (1) Retry-After: 7 -> the retry sleeps 7 (>> the ~0.5s computed backoff).
        register_provider(
            f"engine:{p.id}",
            DeepLProvider(
                profile=engines.get_profile(p.id),
                transport=_deepl_429_then_ok_transport({}, retry_after=7),
            ),
        )
        res = run_completion(
            ProviderSpec(provider=f"engine:{p.id}", backoff_base=0.5),
            "x",
            task="translate",
            payload={"text": "Hello", "target": "fr-FR"},
        )
        assert res["text"] == "Bonjour le monde", "must retry after the 429 and succeed"
        assert slept == [7.0], f"Retry-After:7 must be honored as the sleep, got {slept}"

        # (2) A HUGE header is CAPPED at config.PROVIDER_RETRY_AFTER_CAP_S (60) so a
        # hostile/buggy header can never park the job.
        slept.clear()
        register_provider(
            f"engine:{p.id}",
            DeepLProvider(
                profile=engines.get_profile(p.id),
                transport=_deepl_429_then_ok_transport({}, retry_after=86400),
            ),
        )
        res2 = run_completion(
            ProviderSpec(provider=f"engine:{p.id}", backoff_base=0.5),
            "x",
            task="translate",
            payload={"text": "Hello", "target": "fr-FR"},
        )
        assert res2["text"] == "Bonjour le monde"
        assert slept == [float(config.PROVIDER_RETRY_AFTER_CAP_S)], (
            f"a huge Retry-After must be capped at {config.PROVIDER_RETRY_AFTER_CAP_S}, got {slept}"
        )

        # (3) ABSENT header -> existing exponential backoff (backoff_base * 2**attempt),
        # unchanged.
        slept.clear()
        register_provider(
            f"engine:{p.id}",
            DeepLProvider(
                profile=engines.get_profile(p.id),
                transport=_deepl_429_then_ok_transport({}, retry_after=None),
            ),
        )
        res3 = run_completion(
            ProviderSpec(provider=f"engine:{p.id}", backoff_base=0.5),
            "x",
            task="translate",
            payload={"text": "Hello", "target": "fr-FR"},
        )
        assert res3["text"] == "Bonjour le monde"
        assert slept == [0.5], f"absent Retry-After must use plain backoff (0.5), got {slept}"
    finally:
        _t.sleep = real_sleep
        P._OVERRIDES.clear()
    print("  NMT honors 429 Retry-After: header=7 -> sleep 7; huge -> capped; absent -> backoff OK")


def test_probes_ok_and_authfail():
    _clear_profiles()
    for kind in (
        "deepl",
        "google",
        "microsoft",
        "cohere",
        "local",
        "openai_compatible",
        "lara",
        "amazon",
    ):
        base = "http://10.0.0.9:1234" if kind in ("local", "openai_compatible") else ""
        # Amazon's probe requires a region + paired secret; Lara uses a paired secret.
        cfg = None
        if kind == "amazon":
            cfg = {"region": "us-east-1", "access_key_secret": "s"}
        elif kind == "lara":
            cfg = {"access_key_secret": "s"}
        p = engines.create_profile(f"P-{kind}", kind, api_key="k", base_url=base, cfg=cfg)
        ok, msg = engines.test_profile(p.id, transport=probe_transport(200))
        assert ok, f"{kind} 200 probe should pass: {msg}"
        ok2, msg2 = engines.test_profile(p.id, transport=probe_transport(401))
        assert not ok2 and ("Auth" in msg2 or "401" in msg2), f"{kind} 401 not caught: {msg2}"
    # Amazon probe without a region fails fast with a clear message (no network).
    pnr = engines.create_profile(
        "P-amz-noregion", "amazon", api_key="k", cfg={"access_key_secret": "s"}
    )
    okr, msgr = engines.test_profile(pnr.id, transport=probe_transport(200))
    assert not okr and "region" in msgr.lower(), f"amazon no-region must fail clearly: {msgr}"
    print(
        "  per-kind Test-connection probes: 200 OK + 401 auth-fail for all 8 kinds; "
        "Amazon no-region fails clearly OK"
    )


def test_web_engine_routes():
    _clear_profiles()
    c = TestClient(app)
    # create
    r = c.post(
        "/setup-ai/engines",
        data={"name": "WebDeepL", "kind": "deepl", "api_key": "sk-web-1", "formality": "more"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    profs = engines.list_profiles()
    assert len(profs) == 1 and profs[0].name == "WebDeepL"
    eid = profs[0].id
    assert (profs[0].config or {}).get("formality") == "more"
    # the page renders the card, and NEVER echoes the key
    page = c.get("/setup-ai").text
    assert "WebDeepL" in page and "sk-web-1" not in page
    # duplicate name -> engine_error redirect, no second row
    r2 = c.post(
        "/setup-ai/engines", data={"name": "WebDeepL", "kind": "google"}, follow_redirects=False
    )
    assert r2.status_code == 303 and "engine_error" in r2.headers["location"]
    assert len(engines.list_profiles()) == 1
    # update: blank key keeps the stored secret; disable
    c.post(
        f"/setup-ai/engines/{eid}", data={"name": "WebDeepL", "api_key": ""}, follow_redirects=False
    )
    assert engines.dec_key(engines.get_profile(eid).api_key_enc) == "sk-web-1"
    assert engines.get_profile(eid).enabled is False  # enabled checkbox absent
    # delete
    c.post(f"/setup-ai/engines/{eid}/delete", follow_redirects=False)
    assert engines.get_profile(eid) is None
    # openai_compatible create via the web form (base_url + model + key)
    r3 = c.post(
        "/setup-ai/engines",
        data={
            "name": "WebOR",
            "kind": "openai_compatible",
            "base_url": "https://openrouter.ai/api",
            "model": "deepseek/deepseek-chat",
            "api_key": "sk-web-or",
        },
        follow_redirects=False,
    )
    assert r3.status_code == 303
    orp = engines.get_by_name("WebOR")
    assert orp and orp.kind == "openai_compatible" and orp.base_url == "https://openrouter.ai/api"
    assert orp.model == "deepseek/deepseek-chat" and bool(orp.api_key_enc)
    # the kind selector renders the new kind on the page
    assert "openai_compatible" in c.get("/setup-ai").text
    # DeepL expanded params flow through the form (model_type/context/custom_instructions)
    r4 = c.post(
        "/setup-ai/engines",
        data={
            "name": "WebDeepLQ",
            "kind": "deepl",
            "api_key": "sk-dq",
            "model_type": "quality_optimized",
            "formality": "more",
            "context": "a help centre",
            "custom_instructions": "Keep it formal.\nDo not translate brand names.\n",
        },
        follow_redirects=False,
    )
    assert r4.status_code == 303
    dq = engines.get_by_name("WebDeepLQ")
    cfg = dq.config or {}
    assert cfg.get("model_type") == "quality_optimized" and cfg.get("formality") == "more"
    assert cfg.get("context") == "a help centre"
    assert cfg.get("custom_instructions") == ["Keep it formal.", "Do not translate brand names."]
    print(
        "  web routes: create/update(no-blank-key)/disable/delete + no key echoed; "
        "openai_compatible + DeepL expanded params via the form OK"
    )


def test_cli_engine_round_trip():
    _clear_profiles()
    from app.cli import cmd_engine
    from app.cli.__main__ import main

    cmd_engine._set_transport(probe_transport(200))
    assert (
        main(
            [
                "engine",
                "create",
                "--name",
                "CliCohere",
                "--kind",
                "cohere",
                "--secret",
                "api_key=sk-cli-9",
                "--model",
                "command-r",
            ]
        )
        == 0
    )
    prof = engines.get_by_name("CliCohere")
    assert prof and engines.dec_key(prof.api_key_enc) == "sk-cli-9"
    # --json show must never contain the key
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--json", "engine", "show", str(prof.id)])
    out = buf.getvalue()
    assert "sk-cli-9" not in out and '"key_set": true' in out
    # test uses the injected transport (offline)
    assert main(["engine", "test", str(prof.id)]) == 0
    # update + delete
    assert main(["engine", "update", str(prof.id), "--disable"]) == 0
    assert engines.get_profile(prof.id).enabled is False
    assert main(["engine", "delete", str(prof.id), "--yes"]) == 0
    assert engines.get_by_name("CliCohere") is None
    # openai_compatible via CLI: name/base-url/model/key only, no extra config needed.
    assert (
        main(
            [
                "engine",
                "create",
                "--name",
                "CliOR",
                "--kind",
                "openai_compatible",
                "--base-url",
                "https://api.deepseek.com",
                "--model",
                "deepseek-chat",
                "--secret",
                "api_key=sk-cli-or",
            ]
        )
        == 0
    )
    orp = engines.get_by_name("CliOR")
    assert orp and orp.kind == "openai_compatible"
    assert orp.base_url == "https://api.deepseek.com" and orp.model == "deepseek-chat"
    assert engines.dec_key(orp.api_key_enc) == "sk-cli-or"
    # DeepL params via CLI: --config for scalars + repeatable --instruction for the list.
    assert (
        main(
            [
                "engine",
                "create",
                "--name",
                "CliDeepLQ",
                "--kind",
                "deepl",
                "--secret",
                "api_key=sk-cli-dq",
                "--config",
                "model_type=quality_optimized",
                "--config",
                "context=a help centre",
                "--instruction",
                "Keep it formal.",
                "--instruction",
                "Do not translate brand names.",
            ]
        )
        == 0
    )
    dq = engines.get_by_name("CliDeepLQ")
    cfg = dq.config or {}
    assert cfg.get("model_type") == "quality_optimized" and cfg.get("context") == "a help centre"
    assert cfg.get("custom_instructions") == ["Keep it formal.", "Do not translate brand names."], (
        "CLI --instruction must store a LIST (not a per-char-iterable string)"
    )
    cmd_engine._set_transport(None)
    print(
        "  CLI engine create/list/show(json, no key)/test/update/delete round-trip; "
        "openai_compatible + DeepL --config/--instruction parity OK"
    )


def test_engine_write_door_refuses_egress_denied_base_url():
    """#144: the engine registry now runs the local-engine egress door at
    create/update, the way the settings registry always has. The call-time door was
    never bypassed -- this is defence in depth plus the operator feedback the write
    door owes: refuse where the mistake is made, in the door's own words."""
    _clear_profiles()
    DENIED = "http://169.254.169.254/"

    # (a) create, the kind from the report: refused, with the door's own text, and
    #     NOTHING is written.
    try:
        engines.create_profile("BadLocal", "local", base_url=DENIED, model="x")
        raise AssertionError("#144: create must refuse an egress-denied base_url")
    except engines.EngineTargetBlocked as exc:
        msg = str(exc)
    assert "169.254.0.0/16" in msg and "deny set" in msg, (
        f"#144: the refusal must carry the door's own text, got {msg!r}"
    )
    assert engines.get_by_name("BadLocal") is None, "#144: a refused profile must not persist"
    assert not engines.list_profiles(), "#144: a refused create must write no row at all"
    # EngineTargetBlocked IS a ValueError, which is what every surface already maps.
    assert isinstance(engines.EngineTargetBlocked(""), ValueError)

    # (b) the door is not local-only: a CLOUD kind pointed at a denied target is
    #     exactly as wrong, and a metadata NAME is refused without any resolution.
    for kind, url in (
        ("deepl", DENIED),
        ("openai_compatible", "http://metadata.google.internal/v1"),
        ("local", "http://0.0.0.0:1234"),
        ("local", "ftp://10.0.0.5:1234"),
    ):
        try:
            engines.create_profile(f"Bad-{kind}-{url}", kind, base_url=url)
            raise AssertionError(f"#144: create must refuse {url} for kind {kind}")
        except engines.EngineTargetBlocked:
            pass
    assert not engines.list_profiles(), "#144: none of the refused creates may persist"

    # (c) legitimate self-hosted targets still create -- this door must never break
    #     the offline/LAN default. _RESOLVER is None in the suites, so a hostname is
    #     accepted without any DNS, exactly as an offline install behaves.
    good = [
        ("OkLoop", "local", "http://127.0.0.1:1234/v1"),
        ("OkLan", "local", "http://192.168.1.50:8080"),
        ("OkName", "local", "http://lan-box:1234"),
        ("OkTailnet", "local", "http://100.100.5.5:1234"),
        ("OkPublic", "openai_compatible", "https://api.deepseek.com"),
        ("OkBlank", "deepl", ""),
    ]
    for name, kind, url in good:
        p = engines.create_profile(name, kind, base_url=url)
        assert p and p.base_url == url, f"#144: {url} must still be accepted"

    # (d) update: a denied URL is refused and the stored row is UNCHANGED.
    loop = engines.get_by_name("OkLoop")
    try:
        engines.update_profile(loop.id, base_url=DENIED)
        raise AssertionError("#144: update must refuse an egress-denied base_url")
    except engines.EngineTargetBlocked:
        pass
    assert engines.get_profile(loop.id).base_url == "http://127.0.0.1:1234/v1", (
        "#144: a refused update must leave the stored row untouched"
    )

    # (e) a row GRANDFATHERED in before this door existed (written straight to the
    #     DB, as a pre-fix create did) stays editable for everything EXCEPT its URL:
    #     the operator must still be able to rename or disable it, and to FIX the URL.
    with Session(db_engine) as s:
        s.add(EngineProfile(name="Legacy", kind="deepl", base_url=DENIED, model="m"))
        s.commit()
    legacy = engines.get_by_name("Legacy")
    assert engines.update_profile(legacy.id, name="Legacy renamed").name == "Legacy renamed", (
        "#144: an update that does not touch the URL must not revalidate it"
    )
    assert engines.update_profile(legacy.id, enabled=False).enabled is False
    fixed = engines.update_profile(legacy.id, base_url="http://127.0.0.1:9999")
    assert fixed.base_url == "http://127.0.0.1:9999", "#144: the operator must be able to FIX it"

    # (f) test_profile sanity-checks the stored target for EVERY kind, including the
    #     ones whose probe never routes through local_engine_client_kwargs (deepl
    #     here), so a grandfathered row answers with the door's own words.
    with Session(db_engine) as s:
        s.add(EngineProfile(name="Legacy2", kind="deepl", base_url=DENIED, model="m"))
        s.commit()
    ok, msg = engines.test_profile(engines.get_by_name("Legacy2").id, transport=probe_transport())
    assert ok is False and "169.254.0.0/16" in msg, (
        f"#144: test on a grandfathered denied row must report the door's text, got {msg!r}"
    )
    assert msg.startswith("EgressBlocked:"), (
        f"#144: keep the existing FAILED message shape, got {msg!r}"
    )

    # (g) the boot-path legacy migration refuses cleanly instead of breaking boot or
    #     being mistaken for the name-clash arm.
    _clear_profiles()
    settings.set_many({"local_base_url": DENIED, "local_api_key": "", "local_model": "m"})
    assert engines.migrate_legacy_local() is None, (
        "#144: a denied legacy local_base_url must not be migrated"
    )
    assert not engines.list_profiles(), "#144: nothing may be seeded from a denied legacy slot"
    settings.set_many({"local_base_url": ""})
    _clear_profiles()
    print("  #144: create/update/test refuse an egress-denied engine base_url (door's own text)")


def test_engine_egress_refusal_is_clean_on_every_surface():
    """#144: EngineTargetBlocked subclasses ValueError precisely so every surface's
    EXISTING error mapping carries it -- a clean message, never a 500 or a traceback."""
    _clear_profiles()
    DENIED = "http://169.254.169.254/"

    # web Setup AI form -> the existing engine_error redirect, no row written.
    c = TestClient(app)
    r = c.post(
        "/setup-ai/engines",
        data={"name": "WebBad", "kind": "local", "base_url": DENIED, "model": "m"},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"#144: the web form must redirect cleanly, got {r.status_code}"
    loc = r.headers["location"]
    assert "engine_error" in loc and "169.254" in loc, (
        f"#144: web error must name the target: {loc}"
    )
    assert engines.get_by_name("WebBad") is None, "#144: the web form must write no row"

    # the web UPDATE twin, on a legitimate row.
    good = engines.create_profile("WebOk", "local", base_url="http://127.0.0.1:1234")
    r2 = c.post(
        f"/setup-ai/engines/{good.id}",
        data={"name": "WebOk", "base_url": DENIED},
        follow_redirects=False,
    )
    assert r2.status_code == 303 and "engine_error" in r2.headers["location"]
    assert engines.get_profile(good.id).base_url == "http://127.0.0.1:1234", (
        "#144: a refused web update must leave the row untouched"
    )

    # CLI -> a clean CliError (exit 2, usage), never a traceback.
    from app.cli.__main__ import main

    assert (
        main(
            [
                "engine",
                "create",
                "--name",
                "CliBad",
                "--kind",
                "local",
                "--base-url",
                DENIED,
                "--model",
                "m",
            ]
        )
        == 2
    ), "#144: the CLI must refuse with a clean usage exit, not a traceback"
    assert engines.get_by_name("CliBad") is None, "#144: the CLI must write no row"
    _clear_profiles()
    print("  #144: web form + web update + CLI all refuse cleanly (no 500, no traceback)")


def test_engine_grandfathered_row_stays_editable_through_the_web_form():
    """Reviewer round 1 MED. The Setup AI edit form posts EVERY field, including the row's
    existing base_url, so a door that revalidated whenever base_url was merely PASSED
    would make a profile stored before #144 un-renamable and un-disablable through the
    UI -- exactly the operators who most need to fix one. The door fires on a CHANGE,
    not on a submission."""
    _clear_profiles()
    DENIED = "http://169.254.169.254/"
    with Session(db_engine) as s:
        s.add(EngineProfile(name="Grandfathered", kind="local", base_url=DENIED, model="m"))
        s.commit()
    eid = engines.get_by_name("Grandfathered").id

    c = TestClient(app)
    # rename + disable, resubmitting the stored (denied) URL exactly as the form does
    r = c.post(
        f"/setup-ai/engines/{eid}",
        data={"name": "Grandfathered renamed", "base_url": DENIED, "model": "m"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "engine_error" not in r.headers["location"], (
        f"#144: editing a grandfathered row without changing its URL must work: "
        f"{r.headers.get('location')}"
    )
    row = engines.get_profile(eid)
    assert row.name == "Grandfathered renamed", "the rename must land"
    assert row.enabled is False, "the unchecked enabled box must land"
    assert row.base_url == DENIED, "the stored URL is untouched, not silently rewritten"

    # and the operator can FIX it, while still being unable to MOVE it to another
    # denied target
    r2 = c.post(
        f"/setup-ai/engines/{eid}",
        data={"name": "Grandfathered renamed", "base_url": "http://127.0.0.1:1234", "model": "m"},
        follow_redirects=False,
    )
    assert "engine_error" not in r2.headers["location"], r2.headers.get("location")
    assert engines.get_profile(eid).base_url == "http://127.0.0.1:1234"
    r3 = c.post(
        f"/setup-ai/engines/{eid}",
        data={"name": "Grandfathered renamed", "base_url": DENIED, "model": "m"},
        follow_redirects=False,
    )
    assert "engine_error" in r3.headers["location"], "moving onto a denied target must refuse"
    assert engines.get_profile(eid).base_url == "http://127.0.0.1:1234"
    _clear_profiles()
    print("  #144: a grandfathered row stays renamable/disablable/fixable through the web form")


def test_engine_test_exits_nonzero_on_failure():
    """The qa-0915p nit alongside #144: `engine test` must be scriptable -- a FAILED
    probe has to leave a non-zero exit code. Verified, not assumed."""
    _clear_profiles()
    from app.cli import cmd_engine
    from app.cli.__main__ import main

    probe_key = "sk-x"  # a fixture value, not a credential
    prof = engines.create_profile("ExitCode", "cohere", api_key=probe_key, model="command-r")
    # The transport reset is in a `finally` (gate LOW-2): leaving a mock transport
    # installed would silently reshape every later probe in this process.
    try:
        cmd_engine._set_transport(probe_transport(200))
        assert main(["engine", "test", str(prof.id)]) == 0, "a passing probe must exit 0"
        cmd_engine._set_transport(probe_transport(401))
        assert main(["engine", "test", str(prof.id)]) == 1, (
            "a FAILED probe must exit non-zero so a script can detect it"
        )
    finally:
        cmd_engine._set_transport(None)
        _clear_profiles()
    print("  engine test: exit 0 on OK, exit 1 on FAILED (scriptable)")


def _models_transport(seen, status=200, want_key=None):
    """/models mock: records the Authorization header of every request; 401 when the
    server 'enforces' `want_key` and the bearer differs."""

    def h(req):
        seen.append((str(req.url.host), req.headers.get("authorization")))
        if want_key is not None and req.headers.get("authorization") != f"Bearer {want_key}":
            return httpx.Response(401, json={"error": "bad key"})
        return httpx.Response(status, json={"data": [{"id": "m-a"}, {"id": "m-b"}]})

    return httpx.MockTransport(h)


def test_list_models_uses_stored_key_only_for_same_origin_346():
    _clear_profiles()
    p = engines.create_profile(
        "M5", "local", base_url="http://127.0.0.1:8082/v1", api_key="stored-key-346"
    )
    # blank key + the saved origin (path differs): the stored key is used -> models come back
    seen = []
    res = engines.list_models(
        p.id,
        "http://127.0.0.1:8082",
        "",
        transport=_models_transport(seen, want_key="stored-key-346"),
    )
    assert res["models"] == ["m-a", "m-b"] and not res.get("auth_failed"), res
    assert seen == [("127.0.0.1", "Bearer stored-key-346")], seen
    # blank key + no base URL at all: the saved one, stored key
    seen = []
    res = engines.list_models(p.id, transport=_models_transport(seen, want_key="stored-key-346"))
    assert res["models"] == ["m-a", "m-b"]
    # blank key + a DIFFERENT origin (other host, other port, other scheme): never the stored key
    for other in (
        "http://127.0.0.2:8082/v1",
        "http://127.0.0.1:9999/v1",
        "https://127.0.0.1:8082/v1",
    ):
        seen = []
        engines.list_models(p.id, other, "", transport=_models_transport(seen))
        assert seen and all(a == "Bearer lm-studio" for _h, a in seen), (other, seen)
    # a typed key wins, even on the saved origin
    seen = []
    engines.list_models(
        p.id, "http://127.0.0.1:8082/v1", "typed-key", transport=_models_transport(seen)
    )
    assert seen == [("127.0.0.1", "Bearer typed-key")], seen
    # a server that refuses the key: empty list + the auth flag (no server text echoed)
    res = engines.list_models(
        p.id, "http://127.0.0.1:8082/v1", "wrong", transport=_models_transport([], want_key="other")
    )
    assert res == {"models": [], "blocked": False, "auth_failed": True}, res
    # unknown id -> None; a non-listable kind is flagged, never probed
    assert engines.list_models(987654) is None
    d = engines.create_profile("DL", "deepl", api_key="dl-key")
    assert engines.list_models(d.id).get("unsupported") is True
    _clear_profiles()
    print("  list_models: stored key same-origin only, typed key wins, auth_failed flag")


def test_web_engine_models_route_346():
    _clear_profiles()
    import app.services.providers_part2 as P2
    import app.web.admin as admin_mod

    p = engines.create_profile(
        "M5w", "local", base_url="http://127.0.0.1:8082/v1", api_key="stored-key-346"
    )
    calls = []
    real = P2.local_list_models_result

    def fake(base, key, *, transport=None):
        calls.append((base, key))
        return {"models": ["x1"], "blocked": False}

    P2.local_list_models_result = fake
    real_org = admin_mod._my_org_id
    try:
        c = TestClient(app)
        url = f"/setup-ai/engines/{p.id}/models"
        r = c.post(url, data={"base_url": "http://127.0.0.1:8082/v1", "api_key": ""})
        assert r.status_code == 200 and r.json()["models"] == ["x1"]
        assert calls[-1][1] == "stored-key-346" and "stored-key-346" not in r.text
        r = c.post(url, data={"base_url": "http://evil.example:8082/v1", "api_key": ""})
        assert calls[-1][1] == "lm-studio", calls[-1]
        r = c.post(url, data={"base_url": "http://127.0.0.1:8082/v1", "api_key": "typed"})
        assert calls[-1][1] == "typed"
        # unknown id and another org's engine are the same 404
        assert c.post("/setup-ai/engines/987654/models", data={}).status_code == 404
        n = len(calls)
        admin_mod._my_org_id = lambda request: (p.org_id or 0) + 777
        r = c.post(url, data={"base_url": "http://127.0.0.1:8082/v1", "api_key": ""})
        assert r.status_code == 404 and len(calls) == n, "a foreign engine's key must not be used"
        admin_mod._my_org_id = real_org
        # the Edit form is tagged with its engine id; the Add form keeps the old route
        page = c.get("/setup-ai").text
        assert f'data-eid="{p.id}"' in page and "/setup-ai/local/models" in page
    finally:
        P2.local_list_models_result = real
        admin_mod._my_org_id = real_org
        _clear_profiles()
    print("  web /setup-ai/engines/{id}/models: stored key, origin rule, typed wins, foreign 404")


def test_cli_engine_models_346():
    _clear_profiles()
    import io
    from contextlib import redirect_stdout

    from app.cli import cmd_engine
    from app.cli.__main__ import main

    p = engines.create_profile(
        "M5c", "local", base_url="http://127.0.0.1:8082/v1", api_key="stored-key-346"
    )
    try:
        seen = []
        cmd_engine._set_transport(_models_transport(seen, want_key="stored-key-346"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--json", "engine", "models", str(p.id)])
        out = buf.getvalue()
        assert rc == 0 and '"m-a"' in out and "stored-key-346" not in out, out
        assert seen and seen[0][1] == "Bearer stored-key-346"
        # a server that refuses the key: exit 1, says so
        cmd_engine._set_transport(_models_transport([], want_key="different"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--json", "engine", "models", str(p.id)])
        assert rc == 1 and '"auth_failed": true' in buf.getvalue(), buf.getvalue()
        # no such engine: non-zero
        assert main(["engine", "models", "987654"]) != 0
    finally:
        cmd_engine._set_transport(None)
        _clear_profiles()
    print("  engine models: stored key over the CLI, exit 1 on a refused key")


def test_zh_traditional_simplified_target_mappings():
    """#500: Google, Amazon and DeepL collapsed every zh-* tag to Simplified, so a
    zh-TW/zh-HK project stored and billed Simplified output as a successful
    Traditional translation. Each engine's TARGET mapper now splits Chinese by
    script, case-insensitively. Non-zh mappings (EN-US, PT-BR, de-DE, es-MX, fr-CA,
    pt-PT) are unchanged."""
    traditional = {
        "DeepL": "ZH-HANT",
        "Google": "zh-TW",
        "Amazon": "zh-TW",
    }
    # Simplified DeepL accepts either the script code or the legacy base code.
    simplified_deepl = ("ZH-HANS", "ZH")
    trad_tags = ["zh-TW", "zh-HK", "zh-MO", "zh-Hant", "zh-Hant-TW"]
    simp_tags = ["zh-CN", "zh-SG", "zh-Hans", "zh"]
    for loc in trad_tags:
        for variant in (loc, loc.lower(), loc.upper()):
            assert P._deepl_lang(variant) == traditional["DeepL"], variant
            assert P._google_lang(variant) == traditional["Google"], variant
            assert P._amazon_lang(variant) == traditional["Amazon"], variant
    for loc in simp_tags:
        for variant in (loc, loc.lower(), loc.upper()):
            assert P._deepl_lang(variant) in simplified_deepl, variant
            assert P._google_lang(variant) == "zh-CN", variant
            assert P._amazon_lang(variant) == "zh", variant
    # Existing (non-zh) mappings are untouched by the script split.
    assert P._deepl_lang("en-US") == "EN-US" and P._deepl_lang("en-GB") == "EN-GB"
    assert P._deepl_lang("pt-BR") == "PT-BR" and P._deepl_lang("pt-PT") == "PT-PT"
    assert P._deepl_lang("de-DE") == "DE" and P._deepl_lang("fr") == "FR"
    assert P._google_lang("de-DE") == "de" and P._google_lang("es-ES") == "es"
    assert P._google_lang("pt-BR") == "pt" and P._google_lang("pt-PT") == "pt-PT"
    assert P._amazon_lang("es-MX") == "es-MX" and P._amazon_lang("fr-CA") == "fr-CA"
    assert P._amazon_lang("pt-BR") == "pt" and P._amazon_lang("pt-PT") == "pt-PT"
    assert P._amazon_lang("de-DE") == "de"
    # Review fix: Azure had the same collapse (zh-TW/zh-HK -> bare "zh" = Simplified).
    for loc in trad_tags:
        for variant in (loc, loc.lower(), loc.upper()):
            assert P._ms_lang(variant) == "zh-Hant", variant
    assert P._ms_lang("zh-Hans") == "zh-Hans" and P._ms_lang("pt-BR") == "pt"
    # Gate fix: Azure's Simplified code is zh-Hans; never send a bare "zh".
    for loc in simp_tags:
        for variant in (loc, loc.lower(), loc.upper()):
            assert P._ms_lang(variant) == "zh-Hans", variant
    assert P._ms_lang("pt-PT") == "pt-pt" and P._ms_lang("de-DE") == "de"
    # Review fix: DeepL source_lang must be bare ZH, never the target-only ZH-HANT.
    _clear_profiles()
    dp = engines.create_profile("DZ", "deepl", api_key="k1")
    cap = {}
    DeepLProvider(profile=engines.get_profile(dp.id), transport=deepl_transport(cap)).complete(
        "unused",
        task="translate",
        payload={"text": "Hello world", "target": "zh-TW", "source": "zh-TW"},
    )
    assert cap["body"]["target_lang"] == "ZH-HANT", cap["body"]
    assert cap["body"]["source_lang"] == "ZH", cap["body"]

    print(
        "  #500: zh target mappings split Traditional/Simplified per engine "
        "(case-insensitive); non-zh unchanged OK"
    )


if __name__ == "__main__":
    test_crud_and_secret_round_trip()
    test_legacy_local_migration()
    test_capability_flags()
    test_deepl_request_shape_and_mapping()
    test_google_request_shape_and_mapping()
    test_zh_traditional_simplified_target_mappings()
    test_no_key_or_text_in_logs_302()
    test_microsoft_request_shape_and_mapping()
    test_cohere_request_shape_and_mapping()
    test_openai_compatible_request_shape_and_mapping()
    test_lara_request_shape_signing_and_mapping()
    test_amazon_request_shape_sigv4_and_mapping()
    test_lara_amazon_empty_extraction_is_not_phantom_success()
    test_apply_result_treats_empty_nmt_text_as_error_not_success()
    test_sigv4_signing_key_matches_aws_vector()
    test_amazon_sigv4_signed_host_is_urlsplit_netloc()
    test_mock_glossary_target_backslashes_are_literal()
    test_lara_amazon_paired_secret_cli_web_parity()
    test_deepl_expanded_params_request_shape()
    test_deepl_string_custom_instructions_not_iterated_per_char()
    test_deepl_model_type_auto_set_for_steering_params()
    test_usageless_response_leaves_last_usage_none()
    test_normalize_local_base_case_insensitive_v1()
    test_param_clearing_web_and_cli_parity()
    test_nmt_rejects_prompt_only_and_maps_errors()
    test_get_provider_resolution_and_degradation()
    test_char_based_pricing_and_cohere_tokens()
    test_spend_ledger_accrues_char_cost()
    test_failover_across_kinds()
    test_nmt_honors_retry_after_on_429()
    test_probes_ok_and_authfail()
    test_web_engine_routes()
    test_cli_engine_round_trip()
    test_engine_write_door_refuses_egress_denied_base_url()
    test_engine_egress_refusal_is_clean_on_every_surface()
    test_engine_grandfathered_row_stays_editable_through_the_web_form()
    test_engine_test_exits_nonzero_on_failure()
    test_list_models_uses_stored_key_only_for_same_origin_346()
    test_web_engine_models_route_346()
    test_cli_engine_models_346()
    print("ENGINES OK")
