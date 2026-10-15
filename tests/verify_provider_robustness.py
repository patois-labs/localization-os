"""Provider / local-engine robustness (offline, hermetic).

Regressions for the fix-c wave — three live-QA bugs on the LOCAL-LLM path:

  T4-01  local "Test connection" must FAIL FAST on an unreachable server (~8s connect,
         NO SDK retries) instead of the old ~2-min hang (120s timeout x 2 retries).
  T1-03  a chat provider with an EMPTY resolved model must raise a CLEAR pre-flight
         error BEFORE any API call, not a raw upstream 400.
  T1-04  an UNKNOWN provider name must RAISE (masks typos/misconfig), while the
         sanctioned no-config Mock / keyless-offline defaults stay unchanged.

All hermetic: httpx.MockTransport for the local server, no key, no network egress.
"""

import json
import os
import sys
import time
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

import httpx

from app import config
from app.services import providers as P


# --- T4-01: local Test-connection fails fast ---------------------------------
def test_local_default_client_connect_is_fast():
    """The DEFAULT local client (used by the streamed translation path) caps CONNECT
    at LOCAL_CONNECT_TIMEOUT_S so a dead server errors fast, while keeping the full
    PROVIDER_TIMEOUT_S READ budget so a healthy long generation is never truncated.
    Pre-fix this was a single 120s timeout on all phases (connect included) -> the
    ~2-min hang."""
    c = P._local_client("http://192.0.2.1:1234", "k")  # TEST-NET, never routed
    connect = getattr(c.timeout, "connect", c.timeout)
    read = getattr(c.timeout, "read", c.timeout)
    assert connect == P.LOCAL_CONNECT_TIMEOUT_S == 8, connect
    assert read == config.PROVIDER_TIMEOUT_S, (read, config.PROVIDER_TIMEOUT_S)
    print("  default local client: connect capped 8s, read keeps full 120s budget")


def test_local_test_connection_fails_fast_no_retry():
    """local_complete (the "Test connection" button seam) must error ONCE and fast on
    an unreachable server: NO SDK retries (call count 1, not 3) and a short connect
    budget. Simulated with a MockTransport that raises ConnectTimeout, so no packet
    leaves the box. Pre-fix: default max_retries=2 -> transport hit 3x, and a 120s
    connect timeout per attempt -> the ~2-min hang."""
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        raise httpx.ConnectTimeout("simulated unreachable local server", request=req)

    transport = httpx.MockTransport(handler)
    start = time.time()
    raised = False
    try:
        P.local_complete(
            "http://192.0.2.1:1234",
            "lm-studio",
            "some-model",
            "Translate to French: Hello world",
            transport=transport,
        )
    except Exception:
        raised = True
    elapsed = time.time() - start
    assert raised, "unreachable local test-connection should error, not hang/succeed"
    assert calls["n"] == 1, f"SDK must not retry the test path (got {calls['n']} attempts)"
    assert elapsed < 15, f"test-connection must fail fast (took {elapsed:.1f}s)"
    print(f"  local test-connection failed fast in {elapsed:.2f}s, 1 attempt (no retries)")


# --- T1-03: empty resolved model raises before the API -----------------------
def test_empty_model_raises_before_api():
    """A chat provider whose resolved model is blank/whitespace raises NoModelConfigured
    at pre-flight — before constructing a client or making any request (the guard is
    the first line of complete(), so this runs with NO key and NO network). Pre-fix
    the empty model reached the API and came back a raw 400 that failed the run."""
    for name, cls in (
        ("claude", P.ClaudeProvider),
        ("gemini", P.GeminiProvider),
        ("openai", P.OpenAIProvider),
    ):
        raised = None
        try:
            cls().complete("hello", model="   ")  # whitespace -> resolves empty
        except P.NoModelConfigured as e:
            raised = e
        assert raised is not None, f"{name}: empty model should raise NoModelConfigured"
        # Clean, actionable message + surfaces via the ProviderCallError channel + is
        # terminal (no wasted retries), exactly like the 400 it replaces.
        assert name in str(raised), str(raised)
        assert isinstance(raised, P.ProviderCallError)
        assert P._is_terminal_error(raised) is True
    print("  empty-model chat call -> NoModelConfigured (pre-flight, terminal)")


def test_empty_model_via_empty_env_default():
    """The live-QA repro exactly: step.model unset AND DEFAULT_<X>_MODEL empty -> the
    resolved model is '' -> clean error, not a raw 400."""
    saved = config.DEFAULT_MODELS.get("claude")
    config.DEFAULT_MODELS["claude"] = ""
    try:
        raised = None
        try:
            P.ClaudeProvider().complete("hello")  # no model kw, empty default
        except P.NoModelConfigured as e:
            raised = e
        assert raised is not None, "empty DEFAULT_CLAUDE_MODEL should raise"
        assert "DEFAULT_CLAUDE_MODEL" in str(raised), str(raised)
    finally:
        config.DEFAULT_MODELS["claude"] = saved
    print("  empty DEFAULT_<X>_MODEL -> NoModelConfigured naming the env var")


# --- T1-04: unknown provider raises; no-config Mock preserved -----------------
def test_unknown_provider_name_raises():
    """A NAME that matches nothing known raises UnknownProviderError (a DeadEngineError
    subclass, so every run site surfaces it cleanly). Pre-fix ANY unknown name
    silently returned MockProvider, masking typos with fake [xx] output."""
    raised = None
    try:
        P.get_provider("notaprovider")
    except P.UnknownProviderError as e:
        raised = e
    assert raised is not None, "unknown provider name must raise, not fall back to mock"
    # Rides the existing DeadEngineError perimeter (terminal + caught at every run site).
    assert isinstance(raised, P.DeadEngineError)
    assert P._is_terminal_error(raised) is True
    # A misspelled local-engine name is the exact footgun this closes.
    raised2 = None
    try:
        P.get_provider("local-lm")
    except P.UnknownProviderError as e:
        raised2 = e
    assert raised2 is not None
    print("  unknown provider name -> UnknownProviderError (no silent mock)")


def test_no_config_defaults_still_return_mock():
    """The offline posture the whole hermetic suite depends on must be UNCHANGED: a
    keyless built-in, the explicit 'mock', and the zero-arg default all resolve to
    MockProvider — none of them trip the unknown-name error."""
    assert not config.ANTHROPIC_API_KEY  # keyless in this test env
    assert isinstance(P.get_provider("claude"), P.MockProvider), "keyless built-in -> mock"
    assert isinstance(P.get_provider("openai"), P.MockProvider)
    assert isinstance(P.get_provider("local"), P.MockProvider)
    assert isinstance(P.get_provider("mock"), P.MockProvider), "explicit mock"
    assert isinstance(P.get_provider(None), P.MockProvider), "zero-config default"
    assert isinstance(P.get_provider(""), P.MockProvider)
    print("  keyless built-ins / mock / no-config default -> MockProvider (unchanged)")


def test_registered_override_bypasses_unknown_check():
    """A test/hot-swap override wins by exact name even if the name is otherwise
    unknown — the override seam is checked first, so registering doesn't raise."""
    P.register_provider("some_custom_engine", P.MockProvider())
    assert isinstance(P.get_provider("some_custom_engine"), P.MockProvider)
    print("  registered override bypasses the unknown-name guard")


def test_genai_sdk_error_mapping():
    from google.genai import errors

    for code in (400, 401, 403, 404, 422, 408, 429, 500, 503):
        try:
            errors.APIError.raise_error(code, {"error": {"code": code, "message": "fixture"}}, None)
        except errors.APIError as exc:
            assert P._is_terminal_error(exc) is (code in (400, 401, 403, 404, 422))
        else:
            raise AssertionError(f"SDK did not raise for {code}")
    print("  GenAI APIError.code maps terminal errors and preserves retryable errors")


def test_genai_sdk_stream_contract():
    from unittest.mock import patch

    from google.genai import types

    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(parts=[types.Part(text="translated")]),
                finish_reason="STOP",
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=11,  # pregate: allow-secret token count fixture, not a credential
            candidates_token_count=3,  # pregate: allow-secret token count fixture, not a credential
            cached_content_token_count=2,  # pregate: allow-secret token count, not a credential
        ),
    )
    with patch("google.genai.Client") as client:
        client.return_value.models.generate_content_stream.return_value = iter([response])
        provider = P.GeminiProvider()
        assert (
            provider.complete(
                "prompt",
                system="system",
                model="gemini-2.5-pro",
                max_tokens=100,  # pregate: allow-secret token count fixture, not a credential
                images=[b"png"],
            )
            == "translated"
        )
        assert client.call_args.kwargs["http_options"] == {
            "timeout": config.PROVIDER_TIMEOUT_S * 1000
        }
        # google-genai 2.22.0 HttpOptions.timeout docstring: milliseconds.
        http_options = types.HttpOptions(**client.call_args.kwargs["http_options"])
        assert http_options.timeout == config.PROVIDER_TIMEOUT_S * 1000
        args = client.return_value.models.generate_content_stream.call_args.kwargs
        assert args["config"].system_instruction == "system"
        assert args["config"].max_output_tokens == 100
        assert args["contents"][0].inline_data.data == b"png"
        assert args["contents"][0].inline_data.mime_type == "image/png"
        assert args["contents"][1] == "prompt"
        assert provider.last_usage == {
            # 11 prompt tokens include the 2 cached (#506)
            "input_tokens": 9,  # pregate: allow-secret token count fixture, not a credential
            "output_tokens": 3,  # pregate: allow-secret token count fixture, not a credential
            "cache_read_tokens": 2,  # pregate: allow-secret token count fixture
            "cache_write_tokens": 0,  # pregate: allow-secret token count fixture
        }
    print("  GenAI SDK config, image parts, streamed text and usage contract")


# --- #437: the per-call local engine client must always be closed -------------
class _ClientTracker:
    """Records every httpx.Client constructed while active. The OpenAI SDK never
    closes a caller-supplied http_client (#437), so tracking the *constructed*
    client (not the SDK wrapper) is what proves the call site closed the pool."""

    def __init__(self):
        self.built = []
        self._real = httpx.Client

    def __enter__(self):
        # A SUBCLASS (not a factory): the OpenAI SDK isinstance-checks the supplied
        # http_client against httpx.Client, so the patched name must stay a type.
        real, built = self._real, self.built

        class _TrackingClient(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built.append(self)

        self._patcher = patch.object(httpx, "Client", _TrackingClient)
        self._patcher.__enter__()
        return self

    def __exit__(self, *exc):
        self._patcher.__exit__(*exc)
        return False

    def open_clients(self):
        return [c for c in self.built if not c.is_closed]


def _local_provider(handler):
    """A profile-bound LocalProvider whose wire is an httpx.MockTransport (no egress).
    127.0.0.1 passes the #354 local-engine door; the injected transport is used verbatim."""
    prov = P.LocalProvider()
    prov._profile_base = "http://127.0.0.1:5999"
    prov._profile_key = "lm-studio"
    prov._transport = httpx.MockTransport(handler)
    return prov


def _chat_sse(text):
    """One streamed chat-completion chunk carrying the answer + usage."""
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def test_local_provider_closes_client_after_success():
    """LocalProvider.complete() returns -> the httpx.Client it built this call is CLOSED
    (its connection pool included). Pre-fix the SDK kept the caller-supplied client open
    forever and a provider built per segment leaked one pool per segment."""
    prov = _local_provider(lambda req: _chat_sse("bonjour"))
    tracker = _ClientTracker()
    with tracker:
        out = prov.complete("hello", model="test-model")
    assert out == "bonjour", out
    assert tracker.built, "the call must build its local engine client"
    assert not tracker.open_clients(), "complete() returned but left the client open"
    print(f"  local complete() success: {len(tracker.built)} client built, 0 left open")


def test_local_provider_closes_client_after_error():
    """Same on the failure path: an upstream HTTP 400 (not retried by the SDK) raises
    out of complete() and the client built for that call is still closed."""
    prov = _local_provider(
        lambda req: httpx.Response(
            400, json={"error": {"message": "no", "type": "invalid_request_error"}}
        )
    )
    tracker = _ClientTracker()
    raised = False
    with tracker:
        try:
            prov.complete("hello", model="test-model")
        except Exception:
            raised = True
    assert raised, "HTTP 400 must raise out of complete()"
    assert not tracker.open_clients(), "complete() raised but left the client open"
    print("  local complete() error: client closed on the raise path too")


def test_ten_local_completions_leave_zero_open_clients():
    """The leak's shape from the issue: ten sequential complete() calls (one provider per
    segment is the production pattern) open ten pools and keep ZERO of them open."""
    prov = _local_provider(lambda req: _chat_sse("bonjour"))
    tracker = _ClientTracker()
    with tracker:
        for _ in range(10):
            assert prov.complete("hello", model="test-model") == "bonjour"
    assert len(tracker.built) == 10, len(tracker.built)
    open_after_10 = tracker.open_clients()
    assert not open_after_10, f"{len(open_after_10)} pools left open after 10 calls"
    print("  10 local complete() calls: 10 clients built, 0 left open")


def test_local_test_connection_closes_client():
    """local_complete (the Setup AI 'Test connection' button) is one-off by design: the
    client it builds must be closed before it returns."""

    def handler(req):
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "bonjour"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    tracker = _ClientTracker()
    with tracker:
        out = P.local_complete(
            "http://127.0.0.1:5999",
            "lm-studio",
            "test-model",
            "Translate to French: Hello",
            transport=httpx.MockTransport(handler),
        )
    assert out == "bonjour", out
    assert tracker.built and not tracker.open_clients(), "test-connection left its client open"
    print("  local test-connection: client closed before returning")


def test_local_test_connection_pre_call_raise_leaves_no_client():
    """Gate finding (#437): a pre-call step of local_complete that raises (the echo-key
    lookup here) must not strand a client built before the wire call's try/finally."""
    from app.services import secrets_scrub

    def boom():
        raise RuntimeError("key lookup failed")

    tracker = _ClientTracker()
    raised = False
    with tracker, patch.object(secrets_scrub, "known_keys", boom):
        try:
            P.local_complete(
                "http://127.0.0.1:5999",
                "lm-studio",
                "test-model",
                "Translate to French: Hello",
                transport=httpx.MockTransport(lambda req: httpx.Response(500)),
            )
        except RuntimeError:
            raised = True
    assert raised, "the pre-call failure must propagate"
    assert not tracker.open_clients(), "a pre-call raise left a client open"
    print("  local test-connection pre-call raise: no client left open")


def main() -> None:
    test_genai_sdk_error_mapping()
    test_genai_sdk_stream_contract()
    test_local_default_client_connect_is_fast()
    test_local_test_connection_fails_fast_no_retry()
    test_local_provider_closes_client_after_success()
    test_local_provider_closes_client_after_error()
    test_ten_local_completions_leave_zero_open_clients()
    test_local_test_connection_closes_client()
    test_local_test_connection_pre_call_raise_leaves_no_client()
    test_empty_model_raises_before_api()
    test_empty_model_via_empty_env_default()
    test_unknown_provider_name_raises()
    test_no_config_defaults_still_return_mock()
    test_registered_override_bypasses_unknown_check()
    print("PROVIDER ROBUSTNESS OK")


if __name__ == "__main__":
    main()
