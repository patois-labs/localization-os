"""Test fixtures for the Vendor Studio unit suite. Self-contained; run with the main
venv:  .venv\\Scripts\\python.exe -m pytest tools/vendor_studio/tests -q
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Make `import vstudio` resolve without an install (the package dir is the tool root).
_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOL_ROOT not in sys.path:
    sys.path.insert(0, _TOOL_ROOT)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """A fresh DB + fixed secrets per test. Rebuilds the config + engine + serializer so
    nothing leaks between tests."""
    db = os.path.join(tempfile.mkdtemp(prefix="vstudio_t_"), "vstudio.db")
    monkeypatch.setenv("VSTUDIO_DB_PATH", db)
    monkeypatch.setenv("VSTUDIO_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("VSTUDIO_SSO_SECRET", "test-sso-secret")
    monkeypatch.setenv("VSTUDIO_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("VSTUDIO_LOCSTUDIO_URL", "https://loc.example.test")
    monkeypatch.setenv("VSTUDIO_ADVISOR_MODE", "mock")
    # TestClient talks http; a Secure cookie would not round-trip. The loopback-dev opt-out.
    monkeypatch.setenv("VSTUDIO_COOKIE_INSECURE", "1")

    from vstudio import db as vdb
    from vstudio import security
    from vstudio.config import settings

    settings.reload()
    security.reset_serializer()
    vdb.reset_engine()
    vdb.init_db()
    yield settings
    vdb.reset_engine()


@pytest.fixture()
def session(env):
    from vstudio.db import get_session

    with get_session() as s:
        yield s


@pytest.fixture()
def client(env):
    """A TestClient with an authenticated PM session cookie."""
    from fastapi.testclient import TestClient
    from vstudio import security
    from vstudio.app import app

    c = TestClient(app)
    cookie = security.encode_session(
        {"authed": True, "user": "tester", "name": "tester", "role": "PM"}
    )
    c.cookies.set(security.COOKIE_NAME, cookie)
    return c


# --- shared sample contract --------------------------------------------------
SOURCES = ["Hello ⟦x1⟧ world.", "The cat sat on the mat.", "You have 3 messages."]


def sample_contract(
    claim="claim-tok-123", locale="es-ES", *, locked_last=False, locked_blank=False, glossary=None
):
    """A dispatch contract in the CURRENT (un-enriched) shape, optionally with a locked
    last segment (VP-0 field) to prove graceful handling. `locked_blank` models Loc
    Studio's omit_locked_targets flag: the locked row arrives with a BLANK target and must
    return blank-as-dispatched."""
    segs = []
    for i, src in enumerate(SOURCES):
        seg = {
            "id": i + 1,
            "source": src,
            "target": "",
            "state": "needs-translation",
            "placeholders": (["⟦x1⟧"] if "⟦" in src else []),
        }
        if i == 0:
            seg["target"] = "Hola ⟦x1⟧ mundo (MT)."  # a prefilled MT row
        segs.append(seg)
    if locked_last or locked_blank:
        segs[-1]["locked"] = True
        segs[-1]["locked_reason"] = "100% TM, frozen"
        segs[-1]["target"] = "" if locked_blank else "Tienes 3 mensajes."
        segs[-1]["provenance"] = "tm_exact"
    assets = {}
    if glossary is not None:
        assets["glossary"] = glossary
    return {
        "contract_version": "1.0",
        "run_id": 7,
        "step_key": "vendor",
        "step_name": "Vendor review",
        "claim_token": claim,
        "source_locale": "en-US",
        "target_locale": locale,
        "targets": [locale],
        "segments": segs,
        "assets": assets,
    }
