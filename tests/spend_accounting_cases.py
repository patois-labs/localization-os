"""Offline security regressions for issues 300, 301, 303, 304, 305 and 306."""

from types import SimpleNamespace
from unittest.mock import patch

from app.services import providers, settings, spend


def failed_attempts():
    from app.services.providers_common import _raise_truncated

    class Paid(providers.Provider):
        calls = 0

        def __init__(self):
            self.budgets = []

        def complete(self, prompt, **kw):
            self.calls += 1
            budget = kw.get("max_tokens") or 4000
            self.budgets.append(budget)
            self.last_usage = {"input_tokens": 1000, "output_tokens": budget}
            _raise_truncated("partial", budget=budget, model="claude-opus-4-1")

    p = Paid()
    settings.set_many({"spend_cap_usd": "0"})
    before = spend.spent_this_period()
    with patch.object(providers, "get_provider", return_value=p):
        result = providers.run_completion(
            providers.ProviderSpec("claude", model="claude-opus-4-1", backoff_base=0), "hello"
        )
    delta = spend.spent_this_period() - before
    assert p.calls == 3 and delta > 0 and abs(result["cost_usd"] - delta) < 1e-9, result
    assert p.budgets == [4000, 8000, 16000], p.budgets
    assert abs(delta - 0.715) < 1e-9, delta
    settings.set_many({"spend_cap_usd": str(spend.spent_this_period() + 0.001)})
    p.calls = 0
    with patch.object(providers, "get_provider", return_value=p):
        result = providers.run_completion(
            providers.ProviderSpec("claude", model="claude-opus-4-1", backoff_base=0), "hello"
        )
    assert p.calls == 1 and "Spend cap" in result["error"], result
    settings.set_many({"spend_cap_usd": "0"})


def assist_limits():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Role
    from app.services import assist, security
    from tests._csrf import csrf_for

    fixture_password = "Spend-regression-9381!"  # pregate: allow-secret offline fixture
    email = "spend-translator@example.test"
    security.create_user(email, email, Role.TRANSLATOR, fixture_password)
    settings.set_many({"security_auth_required": "1", "assist_provider": "spend-stub"})

    class Paid(providers.Provider):
        calls = 0
        max_chars = 0

        def complete(self, prompt, system=None, **kw):
            self.calls += 1
            self.max_chars = max(self.max_chars, len(prompt) + len(system or ""))
            self.last_usage = {"input_tokens": 100, "output_tokens": 10}
            return "An answer."

    p = Paid()
    providers.register_provider("spend-stub", p)
    try:
        with TestClient(app) as c:
            csrf_for(c)
            r = c.post("/auth/login", data={"email": email, "password": fixture_password})
            assert r.status_code == 200
            csrf_for(c)
            response = c.post(
                "/setup-ai/assist-limits",
                data={"assist_calls_per_minute": "0", "assist_calls_per_day": "0"},
            )
            assert response.status_code == 403
            codes = [
                c.post("/assist/ask", data={"question": "What is this product?"}).status_code
                for _ in range(30)
            ]
            assert 429 in codes and p.calls <= 6, (codes, p.calls)
            assert p.max_chars < 40000, p.max_chars
        system, prompt = assist.build_ask_prompt("", "What is this product?")
        assert "REFERENCE CARDS" in system and "REFERENCE CARDS" not in prompt
    finally:
        providers._OVERRIDES.pop("spend-stub", None)
        settings.set_many({"security_auth_required": "0", "assist_provider": "mock"})


def public_local():
    p = providers.LocalProvider()
    p._profile_base = "https://8.8.8.8/v1"
    assert not spend.is_exempt(p), "public local engine incorrectly exempt"
    p.last_usage = {"input_tokens": 1000}
    assert spend.record_provider(p, "local", "local-model") > 0
    with patch(
        "socket.getaddrinfo",
        return_value=[
            (2, 1, 6, "", ("10.0.0.1", 0)),
            (2, 1, 6, "", ("8.8.8.8", 0)),
        ],
    ):
        p._profile_base = "https://mixed.example.test/v1"
        assert not spend.is_exempt(p)
    with patch("socket.getaddrinfo", side_effect=OSError("unresolved")):
        assert not spend.is_exempt(p)
    for host in ("127.0.0.1", "10.1.2.3", "100.64.0.1", "[fd00::1]"):
        p._profile_base = f"http://{host}/v1"
        assert spend.is_exempt(p), host


def direct_missing_usage():
    from app.services import glossary_hub, term_extraction

    class Paid(providers.Provider):
        def complete(self, prompt, **kw):
            self.last_usage = None
            return "[]"

    p = Paid()
    for call in (
        lambda: glossary_hub.build_from_text("technical terminology " * 20, p),
        lambda: glossary_hub._choose_sense(
            "technical terminology",
            [
                SimpleNamespace(
                    sense_label="technical",
                    context_cue="technical",
                    target_term="terme",
                )
            ],
            provider=p,
        ),
        lambda: term_extraction._prune(["technical"], "technical text", p),
        lambda: term_extraction._propose_translations(["technical"], "en", "fr", p),
    ):
        before = spend.spent_this_period()
        call()
        assert spend.spent_this_period() > before, "direct call without usage was not booked"


def retry_usage_isolation():
    class Paid(providers.Provider):
        calls = 0

        def complete(self, prompt, **kw):
            self.calls += 1
            if self.calls == 1:
                self.last_usage = {"input_tokens": 1000, "output_tokens": 1000}
                raise providers.ProviderTruncated("partial")
            if self.calls == 2:
                assert self.last_usage is None, "stale usage carried into retry"
                raise RuntimeError("not billed")
            self.last_usage = {"input_tokens": 1000, "output_tokens": 1000}
            return "complete"

    p = Paid()
    before = spend.spent_this_period()
    with patch.object(providers, "get_provider", return_value=p):
        result = providers.run_completion(
            providers.ProviderSpec("paid-stub", model="claude-opus-4-1", backoff_base=0), "hi"
        )
    assert result["text"] == "complete" and abs(result["cost_usd"] - 0.06) < 1e-9
    assert abs(spend.spent_this_period() - before - 0.06) < 1e-9


def stream_abort():
    from contextlib import contextmanager

    from app.services.providers_hub import _drain_openai_stream

    def chunks():
        yield SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(delta=SimpleNamespace(content="partial answer"), finish_reason=None)
            ],
        )
        raise providers.ProviderStalled("stream aborted")

    class Paid(providers.Provider):
        def complete(self, prompt, **kw):
            return _drain_openai_stream(chunks())[0]

    before = spend.spent_this_period()
    with patch.object(providers, "get_provider", return_value=Paid()):
        result = providers.run_completion(
            providers.ProviderSpec("paid-stub", retry_max=0),
            "hello" * 30,
            system="instructions" * 30,
        )
    assert result["error"] and result["cost_usd"] > 0
    assert abs(spend.spent_this_period() - before - result["cost_usd"]) < 1e-9

    def text_chunks():
        yield "partial answer"
        raise providers.ProviderStalled("stream aborted")

    @contextmanager
    def stream(**kwargs):
        yield SimpleNamespace(text_stream=text_chunks())

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    before = spend.spent_this_period()
    with (
        patch("anthropic.Anthropic", return_value=client),
        patch.object(providers, "get_provider", return_value=providers.ClaudeProvider()),
    ):
        result = providers.run_completion(
            providers.ProviderSpec("paid-stub", model="claude-opus-4-1", retry_max=0),
            "hello" * 30,
            system="instructions" * 30,
        )
    assert result["error"] and result["cost_usd"] > 0
    assert abs(spend.spent_this_period() - before - result["cost_usd"]) < 1e-9


def assist_daily_budget():
    from app.services import assist_limits
    from app.services.assist_settings_keys_registered import AssistError
    from app.services.egress import CALLER_ACTOR

    actor = CALLER_ACTOR.set("daily-regression")
    # Ten distinct minute windows consume one shared daily allowance.
    try:
        with patch.object(assist_limits.time, "time", return_value=1_800_000_000) as clock:
            for window in range(10):
                clock.return_value = 1_800_000_000 + window * 61
                for _ in range(6):
                    assist_limits.reserve()
            clock.return_value += 61
            try:
                assist_limits.reserve()
            except AssistError as exc:
                assert exc.status == 429 and "daily" in exc.message
            else:
                raise AssertionError("daily allowance did not stop a later minute")
            other = CALLER_ACTOR.set("different-regression-user")
            try:
                assist_limits.reserve()
            finally:
                CALLER_ACTOR.reset(other)
            clock.return_value += 86400
            assist_limits.reserve()
    finally:
        CALLER_ACTOR.reset(actor)


def tenant_cap():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine
    from app.main import app
    from app.models import Organization, Role
    from app.services import apikeys, security
    from tests._csrf import csrf_for

    with Session(engine) as session:
        org = Organization(name="Spend tenant", slug="spend-tenant")
        session.add(org)
        session.commit()
        session.refresh(org)
        org_id = org.id
    fixture_password = "Spend-tenant-9381!"  # pregate: allow-secret offline fixture
    admin = security.create_user(
        "spend-admin@example.test", "Spend admin", Role.ADMIN, fixture_password, org_id=org_id
    )
    _, raw = apikeys.create_key(admin.id, "tenant-spend", scopes="api docs admin")
    settings.set_many({"security_auth_required": "1", "remote_admin_enabled": "1"})
    with TestClient(app) as client:
        csrf_for(client)
        response = client.post(
            "/auth/login",
            data={
                "email": "spend-admin@example.test",
                "password": fixture_password,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        csrf_for(client)
        settings.set_many({"multi_org_enabled": "1", "spend_cap_usd": "7"})
        for value in ("999", "0"):
            response = client.post("/setup-ai/spend", data={"spend_cap_usd": value})
            assert response.status_code == 403, response.status_code
            assert settings.get("spend_cap_usd") == "7"
        headers = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway minted key
        response = client.post(
            "/api/admin/config", headers=headers, json={"settings": {"spend_cap_usd": "0"}}
        )
        assert response.status_code == 403, response.status_code
        assert settings.get("spend_cap_usd") == "7"
        for key in ("assist_calls_per_minute", "assist_calls_per_day"):
            response = client.post(
                "/api/admin/config", headers=headers, json={"settings": {key: "0"}}
            )
            assert response.status_code == 403, (key, response.status_code)
        response = client.post(
            "/setup-ai/assist-limits",
            data={"assist_calls_per_minute": "0", "assist_calls_per_day": "0"},
        )
        assert response.status_code == 403, response.status_code
    settings.set_many({"multi_org_enabled": "1", "security_auth_required": "1"})
    token = settings.REQUEST_DOOR.set(True)
    try:
        try:
            settings.set_many({"spend_cap_usd": "991"})
        except settings.InstallWideRefused:
            pass
        else:
            raise AssertionError("org request changed the install cap")
    finally:
        settings.REQUEST_DOOR.reset(token)
        settings.set_many(
            {
                "multi_org_enabled": "0",
                "security_auth_required": "0",
                "spend_cap_usd": "0",
                "remote_admin_enabled": "0",
            }
        )


def run():
    from tests.spend_round1_cases import CASES

    failures = []
    for case in (
        failed_attempts,
        assist_limits,
        public_local,
        direct_missing_usage,
        tenant_cap,
        retry_usage_isolation,
        stream_abort,
        assist_daily_budget,
        *CASES,
    ):
        settings.set_many({"spend_cap_usd": "0"})
        try:
            case()
            print(f"  security regression {case.__name__}: PASS")
        except Exception as exc:
            import traceback

            failures.append(case.__name__)
            print(f"  security regression {case.__name__}: FAIL {exc}")
            traceback.print_exc()
    assert not failures, failures
