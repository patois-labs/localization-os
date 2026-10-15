"""Local engine fixed deny set and real settings doors, entirely offline."""

import asyncio
import io
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._bootstrap import init_env

init_env("loctms_local_egress_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Role
from app.services import (
    apikeys,
    audit,
    egress,
    engines,
    image_localization,
    ocr,
    providers,
    security,
    settings,
    spend,
    tailnet,
)
from tests._cli_runner import run_cli

BAD = "http://169.254.169.254"
DENY = (
    BAD + "/latest/meta-data",
    "http://metadata.google.internal:1234",
    "http://[fe80::1]:1234",
    "http://0.0.0.0:1234",
    "http://239.1.1.1:1234",
    "ftp://box:1234",
    "http://[::]:1234",
    "http://[ff02::1]:1234",
    "http://240.0.0.1",
    "http://[fd00:ec2::254]",
    "http://[::ffff:169.254.169.254]",
    "http://[::169.254.169.254]/",
    "http://[64:ff9b::a9fe:a9fe]/",
    "http://[::ffff:0:169.254.169.254]/",
    "http://metadata",
    "http://METADATA.GOOG.",
    "http://0xa9fea9fe",
    "http://METADATA.GOOGLE.INTERNAL.",
)
GOOD = (
    "http://127.0.0.1:1234",
    "http://localhost:1234",
    "http://192.168.1.50:1234",
    "http://10.1.2.3:1234",
    "http://100.64.0.5:1234",
    "https://my-pc.tail1234.ts.net/v1",
    "http://[::1]:1234",
    "http://[fc00::1]:1234",
    "http://box.local:1234",
    "http://100.100.100.200:1234",
    "http://[fd00::5]:11434",
    "http://172.16.0.5:1234",
    "http://lm.lan:1234",
    "https://api.example.test/v1",
    "http://127.1:1234",
)


def refuses(fn, error=egress.EgressBlocked):
    try:
        fn()
    except error as exc:
        return str(exc)
    raise AssertionError("metadata target accepted")


def deny_policy():
    for url in DENY:
        assert (
            "blocked" in refuses(lambda url=url: egress.validate_local_engine_target(url)).lower()
        )
    refuses(
        lambda: egress.validate_local_engine_target(
            "http://box:1234", resolver=lambda h: ["10.0.0.1", "169.254.169.254"]
        )
    )


def allow_policy():
    for url in GOOD:
        assert egress.validate_local_engine_target(url) == url

    def unavailable(host):
        raise OSError("offline")

    assert egress.validate_local_engine_target("http://lan-box", resolver=unavailable)


def deadline_bounded_dns():
    """Reviewer round 1 HIGH, corrected twice by the gate (MED-1, then R2-MED-1).

    `socket.getaddrinfo` takes no timeout, and the engine create/update door runs from an
    `async def` /api route, where a stalled resolver blocks the single-process event loop.
    So THAT door -- and only that door -- passes a deadline. Giving up reads as
    unresolvable, which this door ALLOWS, so a bounded check FAILS OPEN: at worst a bad
    target is SAVED, exactly the pre-#144 state, and the unbounded check before any byte
    leaves still refuses it. Every other caller must stay unbounded and fail-closed.

    The resolver here sleeps PAST the shipped deadline (patched DOWN so the suite stays
    quick), so a regression that bounds every caller shows up as an ALLOW rather than
    passing silently -- the first version of this test slept UNDER the constant and was
    green on the broken tree. The spy on `_resolve_within` pins the same invariant
    structurally, without grepping source for a word a comment can carry.
    """
    import time as _time

    with patch.object(egress, "LOCAL_ENGINE_RESOLVE_DEADLINE_S", 0.2):

        def slow_and_hostile(host):
            _time.sleep(0.7)  # comfortably past the (patched) 0.2s deadline
            return ["169.254.169.254"]

        def spy():
            return patch.object(egress, "_resolve_within", wraps=egress._resolve_within)

        # (1) THE DEFAULT: no deadline -> waits, then refuses what it was told, and never
        #     reaches the bounded helper at all.
        with spy() as seen:
            refuses(
                lambda: egress.validate_local_engine_target(
                    "http://slow-box:1234", resolver=slow_and_hostile
                )
            )
            assert not seen.called, "an unbounded check must not reach the deadline helper"

        # (2) THE REAL CALL SITE, through the production resolver seam: the check that
        #     decides whether bytes may leave is still strict.
        with patch.object(egress, "_RESOLVER", slow_and_hostile), spy() as seen:
            refuses(lambda: providers.local_engine_client_kwargs("http://slow-box:1234", timeout=1))
            assert not seen.called, "the pre-egress check must never be bounded (gate MED-1)"

        # (3) THE ONE OPT-IN CALLER: bounded, gives up rather than wait, and ALLOWS --
        #     the fail-open cost this door accepts, in exchange for not stalling the loop.
        with patch.object(egress, "_RESOLVER", slow_and_hostile), spy() as seen:
            started = _time.monotonic()
            assert engines._validate_base_url(
                "http://slow-box:1234", deadline=engines._WRITE_DEADLINE_S()
            )
            elapsed = _time.monotonic() - started
            assert seen.called, "the engine write door must bound its DNS"
            assert elapsed < 0.6, f"the write door must honour its deadline, waited {elapsed:.2f}s"

        # (4) END TO END, and the whole point of the design: under the slow resolver the
        #     CREATE door allows, so the target is SAVED -- and `test_profile`, which
        #     passes no deadline, then refuses the very row that was just written. Saved,
        #     never used. (Run under the production resolver seam, or it proves nothing
        #     about the deadline at all: the suite default is _RESOLVER = None.)
        with patch.object(egress, "_RESOLVER", slow_and_hostile):
            prof = engines.create_profile("SlowResolver", "local", base_url="http://slow-box:1234")
            try:
                assert prof.base_url == "http://slow-box:1234", (
                    "the bounded create door must accept what it could not resolve in time"
                )
                # (5) The probe is the LAST check before a real request, and for the
                #     cloud kinds `_client` has no egress door of its own, so it must be
                #     strict -- it must NOT inherit the write door's deadline (gate
                #     R2-LOW-2, pinned here per R3-LOW-1).
                with spy() as seen:
                    ok, msg = engines.test_profile(prof.id)
                    assert ok is False and "169.254.0.0/16" in msg, (
                        f"test_profile must refuse the stored denied target, got {msg!r}"
                    )
                    assert not seen.called, "the engine test probe must stay unbounded"
            finally:
                with patch.object(egress, "_RESOLVER", None):
                    engines.delete_profile(prof.id)

        # (6) Nor does the settings write door opt in.
        with patch.object(egress, "_RESOLVER", slow_and_hostile), spy() as seen:
            try:
                settings.validate_value("local_base_url", "http://slow-box:1234")
                raise AssertionError("the settings door must refuse a resolved deny-set address")
            except settings.InvalidSettingValue:
                pass
            assert not seen.called, "the settings write door must stay unbounded"

    # A resolver that ANSWERS in time is fully checked either way, in both directions.
    for kw in ({}, {"deadline": 5.0}):
        assert egress.validate_local_engine_target(
            "http://lan-box:1234", resolver=lambda h: ["10.0.0.9"], **kw
        )
        refuses(
            lambda kw=kw: egress.validate_local_engine_target(
                "http://evil-box:1234", resolver=lambda h: ["169.254.169.254"], **kw
            )
        )

    # A resolver that RAISES is unresolvable on both paths -- unchanged, long-standing.
    def raising(host):
        raise OSError("resolver down")

    assert egress.validate_local_engine_target("http://slow-box:1234", resolver=raising)
    assert egress.validate_local_engine_target(
        "http://slow-box:1234", resolver=raising, deadline=0.2
    )


def models_route():
    sent = []

    def forbidden(request):
        sent.append(request)
        raise AssertionError("outbound request escaped")

    real_get = httpx.get

    def mocked_get(url, **kw):
        with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
            return client.get(url, **kw)

    with Session(engine) as s:
        before = len(
            s.exec(select(AuditLog).where(AuditLog.action == "egress.private_target")).all()
        )
    with patch.object(httpx, "get", mocked_get):
        r = TestClient(app).post("/setup-ai/local/models", data={"base_url": BAD})
    assert httpx.get is real_get
    assert r.status_code == 200 and r.json()["blocked"] is True, r.text
    assert r.json()["models"] == [] and "Blocked target:" in r.json()["error"]
    assert sent == []
    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "egress.private_target")).all()
        assert len(rows) == before + 1
        assert json.loads(rows[-1].detail)["subsystem"] == "local_engine"


def settings_validator():
    refuses(lambda: settings.validate_value("local_base_url", BAD), settings.InvalidSettingValue)
    for url in (*GOOD, "", "   ", None):
        settings.validate_value("local_base_url", url)
    settings.set_many({"local_base_url": BAD})  # raw store deliberately remains legal
    assert settings.get("local_base_url") == BAD


def cli_door():
    for url in (BAD, *GOOD, ""):
        # local_base_url is security-sensitive (content egress), so a write confirms.
        code, out, err = run_cli(["config", "set", "local_base_url", url, "--yes"])
        assert (code != 0) == (url == BAD), (code, out, err)
        if url == BAD:
            assert "blocked" in (out + err).lower()


def cli_apply_door():
    path = Path(os.environ["LOC_TMS_DATA_DIR"]) / "local.toml"
    for url in (BAD, *GOOD, ""):
        before = settings.get("local_base_url")
        path.write_text(f'local_base_url = "{url}"\n', encoding="utf-8")
        code, out, err = run_cli(["config", "apply", str(path), "--yes"])
        assert (code != 0) == (url == BAD), (code, out, err)
        assert settings.get("local_base_url") == (before if url == BAD else url)


def web_door():
    for url in (BAD, *GOOD, ""):
        r = TestClient(app).post("/setup-ai/local", data={"base_url": url}, follow_redirects=False)
        assert r.status_code == 303
        assert ("key_error" in r.headers["location"]) == (url == BAD), r.headers
        if url != BAD:
            assert settings.get("local_base_url") == url


def api_door():
    settings.set_many({"remote_admin_enabled": "1"})
    user = security.get_by_email("egress@example.test") or security.create_user(
        "egress@example.test",
        "Egress",
        Role.ADMIN,
        "offline-test-password",  # pregate: allow-secret offline password fixture
    )
    _, raw = apikeys.create_key(user.id, "egress", scopes="api admin")
    client = TestClient(app)
    for url in (BAD, *GOOD, ""):
        r = client.post(
            "/api/admin/config",
            headers={"X-Loc-Token": raw},
            json={"settings": {"local_base_url": url}, "confirm": ["local_base_url"]},
        )
        assert r.status_code == (400 if url == BAD else 200), r.text
        if url == BAD:
            assert "blocked" in r.text.lower()


def mcp_door():
    settings.set_many({"remote_admin_enabled": "1"})
    user = security.get_by_email("egress@example.test")
    _, raw = apikeys.create_key(user.id, "egress-mcp", scopes="api admin")
    client = TestClient(app)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/mcp-server"))
    import server as mcp_server

    def relay(request):
        r = client.request(
            request.method, request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(r.status_code, json=r.json())

    async def exercise():
        for url in (BAD, *GOOD, ""):
            try:
                result = await mcp_server.call_tool_unvalidated(
                    "manage_install",
                    {
                        "action": "set_config",
                        "settings": {"local_base_url": url},
                        "confirm": ["local_base_url"],
                    },
                )
            except Exception as exc:
                assert url == BAD and "blocked" in str(exc).lower(), str(exc)
            else:
                assert url != BAD, result

    with (
        patch.object(mcp_server, "TOKEN", raw),
        patch.object(
            mcp_server, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(relay))
        ),
    ):
        asyncio.run(exercise())


def proxy_order():
    with patch.object(config, "TAILNET_PROXY", "socks5://localhost:1055"):
        kw = providers.local_engine_client_kwargs(GOOD[5], timeout=6)
        assert kw["proxy"] == "socks5://localhost:1055"
        with (
            patch.object(tailnet, "tailnet_client_kwargs") as proxy,
            patch.object(httpx, "Client") as client,
        ):
            refuses(lambda: providers._local_client(BAD))
            proxy.assert_not_called()
            client.assert_not_called()
        with (
            patch.object(egress, "_RESOLVER", lambda host: ["169.254.169.254"]),
            patch.object(tailnet, "tailnet_client_kwargs") as proxy,
            patch.object(httpx, "Client") as client,
        ):
            assert providers.local_list_models_result(GOOD[5])["blocked"] is True
            proxy.assert_not_called()
            client.assert_not_called()


def blank_base():
    with patch.object(audit, "log") as logged:
        for base in ("", "   ", None):
            assert refuses(lambda base=base: egress.validate_local_engine_target(base)) == (
                "No base URL configured (set the server URL in Setup AI)"
            )
        logged.assert_not_called()


def probe_tailnet_routing():
    base = "https://box.ts.net"
    with patch.object(config, "TAILNET_PROXY", "socks5://localhost:1055"):
        signatures = []
        for local in (False, True):
            with patch.object(
                egress, "validate_local_engine_target", wraps=egress.validate_local_engine_target
            ) as door:
                with engines._client(base, local_engine=local) as client:
                    signatures.append(
                        [
                            (p.pattern, type(t).__name__, type(t._pool).__name__)
                            for p, t in client._mounts.items()
                        ]
                    )
                assert door.call_count == int(local)
        assert signatures[0] == signatures[1] == [("all://", "HTTPTransport", "SOCKSProxy")]
        with (
            patch.object(httpx, "Client") as client,
            patch.object(egress, "validate_local_engine_target") as door,
        ):
            client.return_value.__enter__.return_value.get.return_value = httpx.Response(200)
            assert engines._probe_microsoft(SimpleNamespace(base_url=base, config={}), "", None)[0]
            assert client.call_args.kwargs["proxy"] == "socks5://localhost:1055"
            door.assert_not_called()


def runtime_paths():
    sent = []

    def reply(request):
        sent.append(str(request.url))
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
        return httpx.Response(200, json={"data": [{"id": "offline-model"}]})

    transport = httpx.MockTransport(reply)
    with patch.object(config, "TAILNET_PROXY", "socks5://localhost:1055"):
        with patch.object(tailnet, "tailnet_client_kwargs") as proxy:
            for url in GOOD:
                assert providers.local_list_models(url, transport=transport) == ["offline-model"]
                with providers._local_client(url, transport=transport) as client:
                    assert client.models.list().data[0].id == "offline-model"
                prof = SimpleNamespace(base_url=url)
                assert engines._probe_local(prof, "", transport)[0]
                assert engines._probe_openai_compatible(prof, "", transport)[0]
                provider = providers.OpenAICompatibleProvider(transport=transport)
                assert provider.complete("hello", base_url=url) == "hello"
            proxy.assert_not_called()
        sent.clear()
        with (
            patch.object(httpx, "Client") as client,
            patch.object(tailnet, "tailnet_client_kwargs") as proxy,
        ):
            for injected in (None, transport):
                refuses(lambda injected=injected: providers._local_client(BAD, transport=injected))
                assert providers.local_list_models(BAD, transport=injected) == []
                provider = providers.OpenAICompatibleProvider(transport=injected)
                refuses(lambda provider=provider: provider.complete("hello", base_url=BAD))
                for probe in (engines._probe_local, engines._probe_openai_compatible):
                    refuses(
                        lambda probe=probe, injected=injected: probe(
                            SimpleNamespace(base_url=BAD), "", injected
                        )
                    )
            client.assert_not_called()
            proxy.assert_not_called()
        assert not sent
    for model in ("", "offline-model"):
        r = TestClient(app).post(
            "/setup-ai/local/test", data={"base_url": BAD, "local_model": model}
        )
        assert r.status_code == 200 and r.json()["ok"] is False
        assert "blocked" in r.json()["error"].lower()
    # #346: a 401/403 also says auth_failed (a fixed flag, never the server's text).
    for response, extra in (
        (httpx.Response(401), {"auth_failed": True}),
        (httpx.Response(200, content="not json"), {}),
    ):
        result = providers.local_list_models_result(
            GOOD[1], transport=httpx.MockTransport(lambda r, response=response: response)
        )
        assert result == {"models": [], "blocked": False, **extra}, result
    with patch.object(audit, "log", side_effect=BaseException("audit unavailable")):
        assert providers.local_list_models(BAD, transport=transport) == []


def vision_case(path, base, *, blocked=False, socks=False, cloud=False):
    import openai

    sent, clients, errors = [], [], []
    real_openai = openai.OpenAI
    real_door = egress.validate_local_engine_target

    def door(*args, **kwargs):
        try:
            return real_door(*args, **kwargs)
        except egress.EgressBlocked as exc:
            errors.append(exc)
            raise

    def reply(request):
        sent.append(request)
        assert not blocked, "outbound vision request escaped"
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    def sdk(**kwargs):
        if cloud:
            assert kwargs == {"api_key": key}
        client = real_openai(**kwargs)
        clients.append(client)
        stack.callback(client.close)
        # Inspect the real routing before replacing send with an in-process transport.
        if socks:
            mounts = [
                (p.pattern, type(t._pool).__name__) for p, t in client._client._mounts.items()
            ]
            assert mounts == [("all://", "SOCKSProxy")], "missing SOCKS mount"
        mock = httpx.Client(transport=httpx.MockTransport(reply))
        stack.callback(mock.close)
        stack.enter_context(patch.object(client._client, "send", mock.send))
        assert str(client.base_url) == (
            "https://api.openai.com/v1/" if cloud else providers._normalize_local_base(base) + "/"
        )
        assert client.timeout == openai.DEFAULT_TIMEOUT
        assert client.api_key == key
        return client

    key = "offline-vision-key"  # pregate: allow-secret in-process SDK fixture
    with Session(engine) as session:
        before = session.exec(
            select(AuditLog).where(AuditLog.action == "egress.private_target")
        ).all()
    with ExitStack() as stack:
        stack.enter_context(patch.object(config, "LOCAL_BASE_URL", base))
        stack.enter_context(patch.object(config, "LOCAL_API_KEY", key))
        stack.enter_context(patch.object(config, "OPENAI_API_KEY", key))
        stack.enter_context(patch.object(config, "TAILNET_PROXY", "socks5://localhost:1055"))
        stack.enter_context(
            patch.dict(os.environ, {"OPENAI_BASE_URL": "https://api.openai.com/v1"})
        )
        stack.enter_context(patch.object(egress, "_RESOLVER", lambda host: []))
        checked = stack.enter_context(
            patch.object(egress, "validate_local_engine_target", side_effect=door)
        )
        stack.enter_context(patch.object(spend, "would_block", return_value=(False, 0, 0)))
        stack.enter_context(patch.object(spend, "record_usage"))
        stack.enter_context(patch.object(image_localization, "vision_call_override", None))
        stack.enter_context(patch.object(openai, "OpenAI", sdk))
        provider = "openai" if cloud else "local"
        result = (
            ocr.ocr_image(b"offline image", provider)
            if path == "ocr"
            else image_localization._guarded_vision_call(b"offline image", "read", provider)
        )
        if blocked:
            assert errors, f"{path}: no EgressBlocked refusal; sent={len(sent)}; result={result!r}"
            assert len(errors) == 1 and not sent and not clients
            # #125: BOTH vision paths must SURFACE the refusal, never swallow it. OCR
            # already did ("[OCR error: EgressBlocked: ...]") as a string; image
            # localization returned a bare None, so an operator whose engine target the
            # door refused saw only a silently empty result. LOW-3: image localization now
            # returns a TYPED VisionRefusal (never a string sniffed for a prefix), so a real
            # recognized line that happens to start with the marker can't be misclassified.
            refusal_text = result if path == "ocr" else getattr(result, "text", None)
            if path != "ocr":
                assert isinstance(result, image_localization.VisionRefusal), (path, result)
            assert isinstance(refusal_text, str) and "EgressBlocked" in refusal_text, (
                path,
                result,
            )
            assert "Blocked target" in refusal_text, (path, result)
        else:
            assert result == "hello", result
            assert len(sent) == 1 and not errors
            if cloud:
                checked.assert_not_called()
                assert sent[0].url.host == "api.openai.com"
    with Session(engine) as session:
        rows = session.exec(
            select(AuditLog).where(AuditLog.action == "egress.private_target")
        ).all()
        assert len(rows) == len(before) + int(blocked)
        if blocked:
            assert json.loads(rows[-1].detail)["subsystem"] == "local_engine"


def vision_ocr_refusal():
    for base in DENY:
        vision_case("ocr", base, blocked=True)


def vision_localization_refusal():
    for base in DENY:
        vision_case("localization", base, blocked=True)


def vision_localization_offline_skip():
    """#125 boundary: surfacing the refusal must NOT turn the clean keyless/offline skip
    into an error. No engine configured is 'nothing to say', and still returns None."""
    with ExitStack() as stack:
        for name in ("LOCAL_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            stack.enter_context(patch.object(config, name, ""))
        stack.enter_context(patch.object(image_localization, "vision_call_override", None))
        for provider in ("local", "openai", "claude", "gemini"):
            result = image_localization._guarded_vision_call(b"offline image", "read", provider)
            assert result is None, (provider, result)


def vision_refusal_never_becomes_content():
    """#125: the refusal is SURFACED, never CONTENT. It is recorded on the region (the
    persisted geometry contract every image surface reads) and kept out of the region's
    text and out of the document context brief -- either would be translated by the next
    step and typeset into the delivered image."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (48, 48), "white").save(buf, format="PNG")
    png = buf.getvalue()
    # LOW-3: a TYPED refusal, so escalate_recognition classifies by isinstance, not by
    # sniffing the returned text for a prefix.
    refusal_text = image_localization._vision_error_text(
        egress.EgressBlocked("Blocked target: '169.254.169.254' is a private/internal name.")
    )
    refusal = image_localization.VisionRefusal(refusal_text)
    regions = [
        {
            "text": "classical text",
            "confidence": 0.1,
            "bbox_axis_aligned": [4, 4, 20, 12],
            "source": "mock",
        }
    ]
    with ExitStack() as stack:
        stack.enter_context(patch.object(config, "LOCAL_BASE_URL", BAD))
        stack.enter_context(patch.object(image_localization, "vision_call_override", None))
        stack.enter_context(
            patch.object(image_localization, "_guarded_vision_call", return_value=refusal)
        )
        out = image_localization.escalate_recognition(png, regions, "local")
        assert out[0]["text"] == "classical text", out
        assert out[0]["source"] == "mock", out
        assert out[0].get("vlm_error") == refusal_text, out
        assert image_localization.context_brief_for_image(png, "local") is None
    # LOW-3 boundary: a real recognized line that merely LOOKS like an error is NOT a
    # refusal -- it becomes the region text and escalation does not stop.
    with ExitStack() as stack:
        stack.enter_context(patch.object(config, "LOCAL_BASE_URL", BAD))
        stack.enter_context(patch.object(image_localization, "vision_call_override", None))
        stack.enter_context(
            patch.object(
                image_localization,
                "_guarded_vision_call",
                return_value="[vision error: not really an error, just OCR'd text]",
            )
        )
        regions2 = [
            {
                "text": "old",
                "confidence": 0.1,
                "bbox_axis_aligned": [4, 4, 20, 12],
                "source": "mock",
            }
        ]
        out2 = image_localization.escalate_recognition(png, regions2, "local")
        assert out2[0]["text"] == "[vision error: not really an error, just OCR'd text]", out2
        assert out2[0]["source"] == "vlm_recognized", out2
        assert "vlm_error" not in out2[0], out2


def vision_ocr_socks():
    vision_case("ocr", "https://box.ts.net", socks=True)


def vision_localization_socks():
    vision_case("localization", "https://box.ts.net", socks=True)


def vision_positive_controls():
    for path in ("ocr", "localization"):
        for base in GOOD:
            vision_case(path, base, socks=".ts.net" in base)
        vision_case(path, "https://api.openai.com", cloud=True)


def nmt_rebinding():
    """#228: saved public names must be checked again by every NMT adapter."""
    from app.services.providers_hub import _provider_for_profile

    fixture_key = "offline-fixture"  # pregate: allow-secret offline fixture
    try:
        for kind in ("deepl", "cohere", "google", "microsoft", "lara", "amazon"):
            egress.set_test_resolver(lambda host: ["93.184.216.34"])
            profile = engines.create_profile(
                f"rebind-{kind}",
                kind,
                base_url="https://rebind.example",
                api_key=fixture_key,
                model="m",
                cfg={"region": "us-east-1"},
            )
            sent = []
            transport = httpx.MockTransport(
                lambda request, sent=sent: sent.append(request) or httpx.Response(200, json={})
            )
            provider = _provider_for_profile(engines.get_profile(profile.id), transport=transport)
            for address in ("169.254.169.254", "fe80::1", "fd00:ec2::254"):
                egress.set_test_resolver(lambda host, address=address: [address])
                try:
                    provider.complete(
                        "Hello", payload={"source": "en", "target": "fr", "text": "Hello"}
                    )
                except egress.EgressBlocked:
                    pass
                except Exception as exc:
                    raise AssertionError(f"{kind} did not refuse egress: {exc}") from exc
                else:
                    raise AssertionError(f"{kind} accepted blocked target")
                assert not sent, kind
    finally:
        egress.set_test_resolver(None)


def main():
    init_db()
    failures = []
    for check in (
        nmt_rebinding,
        blank_base,
        vision_ocr_refusal,
        vision_localization_refusal,
        vision_localization_offline_skip,
        vision_refusal_never_becomes_content,
        vision_ocr_socks,
        vision_localization_socks,
        vision_positive_controls,
        probe_tailnet_routing,
        deny_policy,
        allow_policy,
        deadline_bounded_dns,
        models_route,
        settings_validator,
        cli_door,
        cli_apply_door,
        web_door,
        api_door,
        mcp_door,
        proxy_order,
        runtime_paths,
    ):
        try:
            check()
        except Exception as exc:
            line = f"RED {check.__name__}: {type(exc).__name__}: {exc}"
            print(line)
            failures.append(line)
        else:
            print(f"PASS {check.__name__}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
    assert not failures, f"{len(failures)} local engine egress checks failed"
    print("LOCAL ENGINE EGRESS OK")


if __name__ == "__main__":
    main()
