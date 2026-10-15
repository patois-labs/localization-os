"""Offline regressions adapted from %TEMP%/f25-gate probes."""

import hashlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlmodel import Session, select

from app.db import engine
from app.models import AssistAllowance
from app.services import assist_limits, providers, settings, spend
from app.services.assist_settings_keys_registered import AssistError
from app.services.egress import CALLER_ACTOR


def tailnet_and_cache():
    p = providers.LocalProvider()
    with patch("socket.getaddrinfo", side_effect=OSError("Fly DNS unavailable")) as dns:
        for host in ("gpu-box.tail1234.ts.net", "100.64.0.5", "[fd7a:115c:a1e0::1]"):
            p._profile_base = f"http://{host}:8080"
            assert spend.is_exempt(p), host
            assert spend.blocked_for(p) is None
        assert dns.call_count == 0
    with (
        patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 0))]) as dns,
        patch("app.services.spend.time.monotonic", return_value=1000) as clock,
    ):
        p._profile_base = "https://cache-round1.example.test/v1"
        assert not spend.is_exempt(p)
        p._profile_base = "http://cache-round1.example.test:8080/other"
        assert not spend.is_exempt(p)
        assert dns.call_count == 1
        clock.return_value += 301
        assert not spend.is_exempt(p)
        assert dns.call_count == 2


def lazy_gemini():
    from app.services.providers_hub import GeminiProvider

    for status in (400, 401, 403, 429):
        for started in (False, True):

            def chunks(started=started, status=status):
                if started:
                    yield SimpleNamespace(text="partial")
                raise RuntimeError(str(status))

            client = SimpleNamespace(
                models=SimpleNamespace(generate_content_stream=lambda **kw: chunks())
            )
            p = GeminiProvider()
            with patch("google.genai.Client", return_value=client):
                try:
                    p.complete("hello", model="gemini-2.5-flash")
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("lazy stream did not raise")
            assert bool(p.last_usage) == started, (status, started, p.last_usage)


def configurable_allowance():
    actor = CALLER_ACTOR.set("round1-settings@example.test")
    try:
        settings.set_many({"assist_calls_per_minute": "1", "assist_calls_per_day": "2"})
        with patch.object(assist_limits.time, "time", return_value=1_800_000_000) as clock:
            assist_limits.reserve()
            try:
                assist_limits.reserve()
            except AssistError as exc:
                assert exc.status == 429
            else:
                raise AssertionError("configured minute limit ignored")
            clock.return_value += 61
            assist_limits.reserve()
            clock.return_value += 61
            try:
                assist_limits.reserve()
            except AssistError as exc:
                assert "daily" in exc.message
            else:
                raise AssertionError("configured daily limit ignored")
            settings.set_many({"assist_calls_per_minute": "0", "assist_calls_per_day": "0"})
            for _ in range(65):
                assist_limits.reserve()
        for value in ("-1", "1.5", "no"):
            try:
                settings.set_many({"assist_calls_per_day": value})
            except settings.InvalidSettingValue:
                pass
            else:
                raise AssertionError("invalid allowance accepted")
    finally:
        CALLER_ACTOR.reset(actor)
        settings.set_many({"assist_calls_per_minute": "6", "assist_calls_per_day": "60"})


def host_bucket():
    settings.set_many({"assist_calls_per_minute": "6"})
    actor = CALLER_ACTOR.set("system")
    try:
        for _ in range(6):
            assist_limits.reserve()
        request = settings.REQUEST_DOOR.set(True)
        try:
            assist_limits.reserve()
        finally:
            settings.REQUEST_DOOR.reset(request)
    finally:
        CALLER_ACTOR.reset(actor)
        settings.set_many({"assist_calls_per_minute": "6"})


def allowance_privacy():
    import hmac

    from app import config
    from app.models import Role
    from app.services import gdpr, security

    email = "round1-private@example.test"
    fixture_password = "Round1-erasure-9381!"  # pregate: allow-secret offline fixture
    user = security.create_user(email, email, Role.TRANSLATOR, fixture_password)
    actor = CALLER_ACTOR.set(email)
    try:
        assist_limits.reserve()
    finally:
        CALLER_ACTOR.reset(actor)
    with Session(engine) as s:
        assert s.get(AssistAllowance, hashlib.sha256(email.encode()).hexdigest()) is None
        expected = hmac.new(
            config.SESSION_SECRET.encode(),
            ("assist-allowance:user:" + email).encode(),
            hashlib.sha256,
        ).hexdigest()
        assert s.get(AssistAllowance, expected) is not None
    ok, message = gdpr.erase_user(user.id, actor="round1-operator")
    assert ok, message
    with Session(engine) as s:
        assert s.get(AssistAllowance, expected) is None


def allowance_retention():
    from app.services import retention

    with Session(engine) as s:
        s.add(
            AssistAllowance(
                actor_hash="old-round1",
                day="2020-01-01",
                minute_started=1,
                minute_calls=1,
                daily_calls=1,
            )
        )
        s.commit()
    settings.set_many({"retention_enabled": "1", "retention_days": "36500"})
    try:
        retention.purge(datetime.utcnow())
    finally:
        settings.set_many({"retention_enabled": "0", "retention_days": "0"})
    with Session(engine) as s:
        assert s.get(AssistAllowance, "old-round1") is None
        assert s.exec(select(AssistAllowance)).first() is not None


def allowance_settings_form():
    from fastapi.testclient import TestClient

    from app.main import app
    from tests._csrf import csrf_for

    try:
        with TestClient(app) as client:
            csrf_for(client)
            response = client.post(
                "/setup-ai/assist-limits",
                data={"assist_calls_per_minute": "12", "assist_calls_per_day": "120"},
            )
            assert response.status_code == 200
            assert settings.get("assist_calls_per_day") == "120"
            assert 'value="120"' in response.text
            response = client.post(
                "/setup-ai/assist-limits",
                data={"assist_calls_per_minute": "-1", "assist_calls_per_day": "0"},
            )
            assert "non-negative integer" in response.text
            assert settings.get("assist_calls_per_day") == "120"
    finally:
        settings.set_many({"assist_calls_per_minute": "6", "assist_calls_per_day": "60"})


CASES = (
    tailnet_and_cache,
    lazy_gemini,
    configurable_allowance,
    host_bucket,
    allowance_privacy,
    allowance_retention,
    allowance_settings_form,
)
