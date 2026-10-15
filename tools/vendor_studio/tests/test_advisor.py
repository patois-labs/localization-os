"""Mock advisor determinism + defensive live-parse."""

from __future__ import annotations

from vstudio import advisor


def test_mock_is_deterministic(env):
    a = advisor.advise(1, "Hello", "Hola  mundo ", [], "es-ES")
    b = advisor.advise(1, "Hello", "Hola  mundo ", [], "es-ES")
    assert a == b
    assert a["available"] is True
    # the deterministic normalization is a REAL, assertable edit.
    assert a["suggested"] == "Hola mundo"


def test_mock_notes_missing_glossary_term(env):
    gl = [{"term": "cat", "target": "gato"}]
    out = advisor.advise(2, "the cat", "el felino", gl, "es-ES")
    assert "gato" in out["rationale"]


def test_live_json_extraction_is_defensive():
    # chatty wrapper around JSON still parses.
    assert advisor._extract_json('Sure! {"suggested":"x","rationale":"y"} done') == {
        "suggested": "x",
        "rationale": "y",
    }
    assert advisor._extract_json("no json here") is None
    assert advisor._extract_json("") is None


def test_live_unavailable_never_raises(monkeypatch, env):
    monkeypatch.setenv("VSTUDIO_ADVISOR_MODE", "live")
    monkeypatch.setenv("VSTUDIO_ADVISOR_BASE_URL", "")
    from vstudio.config import settings

    settings.reload()
    out = advisor.advise(3, "Hi", "Hola", [], "es-ES")
    assert out["available"] is False and out["suggested"] == ""
