"""TelemetrySink port -- suite for composable-core phase 1, Train 1.

Proves the load-bearing contracts of the port that names the prompt-tap WRITE seam
(see app/ports/telemetry.py + the SPEC-ports-phase1 spec Train 1):

  A  SEAM: a registered no-op sink SUPPRESSES the default tap write (get() resolves the
     override, so PromptTapSink.capture is never reached -- no file is created).
  B  OBSERVER PURITY: a registered sink whose emit RAISES cannot break run_completion
     (the translation still returns its text; the tap failure is swallowed).
  C  SECURITY / PORT-LEVEL REDACT: a RECORDING sink registered THROUGH THE PORT, fed by a
     run whose upstream echoes an sk--style key into the response AND by an error-channel
     record, receives records that arrive SCRUBBED. Because the recording sink is NOT
     capture(), a scrubbed record proves the redaction happened in the PORT DISPATCHER
     (telemetry.emit), not inside the tap adapter.
  D  REDACT IDENTITY: redact(redact(r)) IS redact(r) (`is`, not `==`) -- the identity-
     preserving property the double-redact belt relies on.
  E  LAZY-IMPORT INVARIANT: `import app.services.providers` does NOT construct the DB
     engine (subprocess: app.db absent from sys.modules), so the facade import stayed lazy
     and providers' config-only module-scope import graph survives (spec HIGH-1).
  F  local_path (chip task_6f7784ca): a qwen3.5-shaped local model stamps "qwen3.5", a
     qwen3.8-shaped one "qwen3.8", a non-adapter local model "chat", and a NON-local
     provider stamps NOTHING (the field is absent).
  G  DEFAULT-PATH REGRESSION: with NO registered override, a record still lands in the tap
     file exactly as before (byte-neutral default behavior; local_path absent for a cloud
     provider), so naming the port did not perturb the default install.
  H  STRUCTURAL CHECK (Train-2 house-pattern backfill): PromptTapSink and a recording sink
     satisfy the runtime_checkable TelemetrySink Protocol; a class without emit() does NOT
     -- so emit()'s presence is an enforced check, not prose.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, cleared keys). ASCII-only prints. Final
line: TELEMETRY SINK OK.
"""

import os
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_telemetry_")

import json as _json

from app import config, ports
from app.db import init_db
from app.ports import telemetry
from app.services import prompt_tap, settings
from app.services.providers import (
    _OVERRIDES,
    LocalProvider,
    Provider,
    ProviderSpec,
    _tap_local_path,
    register_provider,
    run_completion,
)

init_db()

_FAKE_KEY = "sk-ant-FAKEKEY-SENTINEL-DEADBEEF-0123456789"


# --- helpers -----------------------------------------------------------------
class _RecordingSink:
    """A telemetry sink that just remembers what it was handed (through the port)."""

    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _RaisingSink:
    def emit(self, record):
        raise RuntimeError("sink boom -- must never reach the caller")


class _EchoStub(Provider):
    """A cloud-shaped stub (name != 'local') that returns a fixed string."""

    name = "claude"

    def __init__(self, out="[es] translated"):
        self._out = out

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {"input_tokens": 5, "output_tokens": 3}
        return self._out


class _LocalStub(Provider):
    """A LocalProvider-shaped stub: name == 'local', so _tap_local_path treats it as a
    local engine and derives local_path from the resolved model id."""

    name = "local"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {"input_tokens": 3, "output_tokens": 2}
        return "[es] local out"


def _reset_tap(on=True):
    prompt_tap.clear()
    ad = prompt_tap._actions_dir()
    if ad.exists():
        shutil.rmtree(ad, ignore_errors=True)
    settings.set_many({"prompt_tap_enabled": "1" if on else "0"})


def _run(provider_name, model, prompt="hola", out=None, provider=None):
    """Drive run_completion once with a registered override provider; return the result."""
    if provider is None:
        provider = _EchoStub(out) if out is not None else _EchoStub()
    register_provider(provider_name, provider)
    try:
        spec = ProviderSpec(provider=provider_name, model=model)
        return run_completion(
            spec,
            prompt,
            system="sys",
            task="translate",
            payload={"text": prompt, "target": "es-ES"},
        )
    finally:
        _OVERRIDES.pop(provider_name, None)


# ============================================================================
# A. the seam -- a no-op sink suppresses the default tap write
# ============================================================================
def test_noop_sink_suppresses_the_write():
    _reset_tap(True)
    d = prompt_tap._dir()
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)

    class _Noop:
        def emit(self, record):
            return None

    ports.register("telemetry", _Noop())
    try:
        res = _run("claude", "claude-x")
        assert res["text"] == "[es] translated", res
        # The default PromptTapSink was never reached, so NO capture file exists.
        assert not prompt_tap._path().exists(), (
            "a registered no-op sink must SUPPRESS the default tap write"
        )
        assert prompt_tap.read_records() == [], "no records may have been captured"
    finally:
        ports.unregister("telemetry")
    print(
        "  (A) seam: a registered no-op sink suppresses the default tap write "
        "(get() resolves the override) OK"
    )


# ============================================================================
# B. observer purity -- a raising sink cannot break run_completion
# ============================================================================
def test_raising_sink_never_breaks_run_completion():
    _reset_tap(True)
    ports.register("telemetry", _RaisingSink())
    try:
        res = _run("claude", "claude-x", prompt="bonjour")
        assert res["text"] == "[es] translated" and res["error"] is None, (
            "a sink that raises must not perturb the translation",
            res,
        )
    finally:
        ports.unregister("telemetry")
    print(
        "  (B) observer purity: a sink whose emit() RAISES cannot break run_completion "
        "(text returned, error None) OK"
    )


# ============================================================================
# C. security -- the PORT DISPATCHER redacts before any sink sees the record
# ============================================================================
def test_port_level_redact_scrubs_before_the_sink():
    _reset_tap(True)
    sink = _RecordingSink()
    old = config.ANTHROPIC_API_KEY
    config.ANTHROPIC_API_KEY = _FAKE_KEY
    settings.set_many({"claude_api_key": _FAKE_KEY})
    ports.register("telemetry", sink)
    try:
        # (1) RESPONSE channel: an upstream echoes the key into the model output. The
        # recording sink (NOT capture) must receive it already scrubbed -> proves the
        # redaction is PORT-LEVEL, in telemetry.emit, not inside the tap adapter.
        _run("claude", "claude-x", out=f"translated (debug: token={_FAKE_KEY})")
        assert sink.records, "the run must have reached the port dispatcher"
        got = sink.records[-1]
        blob = _json.dumps(got)
        assert _FAKE_KEY not in blob and "sk-ant-" not in blob, (
            "the record the SINK received must be scrubbed by the port dispatcher",
            got,
        )
        assert got.get("redacted") is True, "a scrub sets the honest redacted marker"

        # (2) ERROR channel: a 401 body carrying the key, built through the port's own
        # re-exported build_record and emitted through the port. Same guarantee.
        sink.records.clear()
        rec = telemetry.build_record(
            provider="claude",
            provider_kind="claude",
            model="m",
            task="translate",
            system="s",
            prompt="p",
            max_tokens=8,
            payload={"text": "p"},
            outcome="error",
            error=f'HTTP 401: {{"message":"Incorrect API key provided: {_FAKE_KEY}"}}',
        )
        telemetry.emit(rec)
        got2 = sink.records[-1]
        assert _FAKE_KEY not in _json.dumps(got2) and "sk-ant-" not in _json.dumps(got2), (
            "an error-channel key must be scrubbed by the port dispatcher",
            got2,
        )

        # (3) H1 twin: the spec's own Train-1 call shape telemetry.get().emit(rec) must ALSO
        # scrub -- get() returns a _RedactingSink wrapper, so a sink reached that way never
        # sees an unredacted record (the invisible-bypass the post-gate fix closes).
        sink.records.clear()
        rec2 = telemetry.build_record(
            provider="claude",
            provider_kind="claude",
            model="m",
            task="translate",
            system="s",
            prompt="p",
            max_tokens=8,
            payload={"text": "p"},
            outcome="ok",
            response_text=f"leaked {_FAKE_KEY} here",
        )
        telemetry.get().emit(rec2)
        got3 = sink.records[-1]
        assert _FAKE_KEY not in _json.dumps(got3) and "sk-ant-" not in _json.dumps(got3), (
            "telemetry.get().emit(rec) must scrub too (get() returns a redacting wrapper)",
            got3,
        )
        assert got3.get("redacted") is True, "the get() path sets the redacted marker"
    finally:
        config.ANTHROPIC_API_KEY = old
        settings.set_many({"claude_api_key": ""})
        ports.unregister("telemetry")
    print(
        "  (C) security: a recording sink registered THROUGH THE PORT receives records "
        "SCRUBBED of sk- material on the response + error channels via BOTH "
        "telemetry.emit AND telemetry.get().emit -- redaction is bound to get(), not "
        "adapter-internal OK"
    )


# ============================================================================
# D. redact identity -- the double-redact belt is byte-identical
# ============================================================================
def test_redact_is_identity_preserving_and_idempotent():
    # A record that DOES get scrubbed: the first redact returns a NEW object; a second
    # redact of that output returns the SAME object (nothing left to change).
    r = telemetry.build_record(
        provider="claude",
        provider_kind="claude",
        model="m",
        task="translate",
        system="s",
        prompt="p",
        max_tokens=8,
        payload={"text": "p"},
        outcome="error",
        error=f"leak {_FAKE_KEY} tail",
    )
    once = prompt_tap.redact(r)
    assert once is not r, "a record with a secret must be scrubbed into a NEW object"
    assert prompt_tap.redact(once) is once, (
        "redact must be IDENTITY-preserving on its own output: redact(redact(r)) IS "
        "redact(r) -- the property the double-redact belt (port + capture) relies on"
    )
    # A clean record is returned UNCHANGED (same object) on the first pass too.
    clean = telemetry.build_record(
        provider="mock",
        provider_kind="mock",
        model="m",
        task="translate",
        system="s",
        prompt="nothing to see",
        max_tokens=8,
        payload={"text": "x"},
        outcome="ok",
        response_text="fine",
    )
    assert prompt_tap.redact(clean) is clean, "a clean record is returned unchanged (is)"
    print(
        "  (D) redact identity: redact(redact(r)) IS redact(r) (new object on a scrub, "
        "same object on a clean record) OK"
    )


# ============================================================================
# E. lazy-import invariant -- importing providers does NOT build the DB engine
# ============================================================================
def test_import_providers_does_not_construct_the_engine():
    code = (
        "import sys, os, tempfile\n"
        "os.environ['LOC_TMS_DATA_DIR'] = tempfile.mkdtemp(prefix='loctms_lazyimp_')\n"
        "import app.services.providers\n"
        "assert 'app.db' not in sys.modules, 'importing providers constructed app.db!'\n"
        # The telemetry facade transitively reaches app.db, so it too must stay unimported
        # by a bare providers import (the swap kept the import lazy, inside run_completion).
        "assert 'app.ports.telemetry' not in sys.modules, 'providers imported the port eagerly!'\n"
        "print('LAZY OK')\n"
    )
    env = dict(os.environ)
    env.pop("LOC_TMS_DATA_DIR", None)
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=_ROOT, env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, (
        "importing app.services.providers must NOT construct the DB engine (spec HIGH-1)\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "LAZY OK" in proc.stdout, proc.stdout
    print(
        "  (E) lazy-import invariant: `import app.services.providers` leaves app.db AND "
        "app.ports.telemetry unimported (facade import stayed lazy) OK"
    )


# ============================================================================
# F. local_path (chip task_6f7784ca)
# ============================================================================
def test_local_path_derivation_helper():
    """The pure derivation, asserted directly: family by resolved model id, gated on a
    LocalProvider (name == 'local')."""
    local = LocalProvider()
    assert _tap_local_path(local, "qwen3.5-9b") == "qwen3.5"
    assert _tap_local_path(local, "qwen3.5-9b-loc-esfr-v1") == "qwen3.5"
    assert _tap_local_path(local, "qwen3.8-27b") == "qwen3.8"
    assert _tap_local_path(local, "local-model") == "chat", "non-adapter local -> chat"
    # A non-local provider is None regardless of a qwen-shaped model id.
    assert _tap_local_path(_EchoStub(), "qwen3.5-9b") is None, "non-local -> None (absent)"

    # L2: the F1-decision EXCLUSIONS (Qwen3-8B family is NOT a raw-adapter family) must
    # resolve to "chat" at the _tap_local_path level too -- a local call, but on the chat
    # path -- never mis-stamped qwen3.8. Pins the composition with _local_prompt_adapter's
    # exclusion so the two can't drift.
    for excluded in ("qwen3-8b", "Qwen3-8B-Q4_K_M", "qwen3.8-base"):
        assert _tap_local_path(local, excluded) == "chat", (
            f"F1-excluded local model {excluded!r} must stamp chat, never a raw family"
        )

    # M1: a legacy single-slot LocalProvider with a BLANK spec model resolves its real
    # model from config.DEFAULT_MODELS['local'] -- so the chip does not stamp "chat" for a
    # call that actually took the raw qwen path.
    old = config.DEFAULT_MODELS.get("local")
    try:
        config.DEFAULT_MODELS["local"] = "qwen3.5-9b-loc-es-v2"
        assert _tap_local_path(LocalProvider(), None) == "qwen3.5", (
            "blank-model local + DEFAULT_MODELS qwen3.5 must resolve to qwen3.5, not chat"
        )
        config.DEFAULT_MODELS["local"] = "qwen3.8-27b"
        assert _tap_local_path(LocalProvider(), None) == "qwen3.8", (
            "blank-model local + DEFAULT_MODELS qwen3.8 must resolve to qwen3.8"
        )
        config.DEFAULT_MODELS["local"] = "local-model"
        assert _tap_local_path(LocalProvider(), None) == "chat", (
            "blank-model local + a non-adapter DEFAULT_MODELS stays chat"
        )
    finally:
        if old is None:
            config.DEFAULT_MODELS.pop("local", None)
        else:
            config.DEFAULT_MODELS["local"] = old
    print(
        "  (F1) local_path helper: qwen3.5/qwen3.8 families, plain local + F1-excluded "
        "Qwen3-8B -> chat, non-local -> None, and the DEFAULT_MODELS blank-model rung OK"
    )


def test_local_path_lands_in_the_record():
    """The chip end-to-end: the derived local_path is stamped on the emitted record for a
    LocalProvider call, and ABSENT for a cloud provider."""
    _reset_tap(True)
    sink = _RecordingSink()
    ports.register("telemetry", sink)
    try:
        for model, expected in (
            ("qwen3.5-9b", "qwen3.5"),
            ("qwen3.8-27b", "qwen3.8"),
            ("local-model", "chat"),
        ):
            sink.records.clear()
            _run("local", model, provider=_LocalStub())
            ok = [r for r in sink.records if r.get("outcome") == "ok"]
            assert ok, ("a local run must have emitted an ok record", model, sink.records)
            assert ok[-1].get("local_path") == expected, (
                f"local_path for {model!r} must be {expected!r}",
                ok[-1].get("local_path"),
            )

        # A NON-local provider stamps NOTHING (the field is absent, not None).
        sink.records.clear()
        _run("claude", "claude-x", provider=_EchoStub())
        ok = [r for r in sink.records if r.get("outcome") == "ok"]
        assert ok, "the cloud run must have emitted an ok record"
        assert "local_path" not in ok[-1], (
            "a non-local provider must NOT stamp local_path (absent by convention)",
            ok[-1].get("local_path"),
        )
    finally:
        ports.unregister("telemetry")
    print(
        "  (F2) local_path in record: qwen3.5/qwen3.8/chat stamped for a LocalProvider "
        "call; ABSENT for a cloud provider OK"
    )


# ============================================================================
# H. structural check -- TelemetrySink is a runtime check, not prose (Train-2 backfill)
# ============================================================================
def test_telemetrysink_is_a_structural_check():
    """House-pattern backfill (mirrors verify_scheduler_port's TimeDriver check): the
    default PromptTapSink and a recording sink satisfy the runtime_checkable TelemetrySink
    Protocol, and -- the load-bearing NEGATIVE control -- a class WITHOUT emit() does NOT.
    That is what makes emit()'s presence an ENFORCED check, not merely prose in the port."""
    assert isinstance(telemetry.PromptTapSink(), telemetry.TelemetrySink)
    assert isinstance(_RecordingSink(), telemetry.TelemetrySink)

    class _NoEmit:
        def other(self): ...

    assert not isinstance(_NoEmit(), telemetry.TelemetrySink), (
        "a sink WITHOUT emit() must FAIL the structural TelemetrySink check -- emit() is the "
        "one required method of the port, not optional prose"
    )
    print(
        "  (H) structural: PromptTapSink + a recording sink satisfy the TelemetrySink "
        "Protocol; a sink without emit() does NOT (a real check, not prose) OK"
    )


# ============================================================================
# G. default-path regression -- the record still lands in the tap file
# ============================================================================
def test_default_path_still_writes_the_record():
    _reset_tap(True)
    assert "telemetry" not in ports._OVERRIDES, "no override for the default-path test"
    res = _run("claude", "claude-x", prompt="hello default")
    assert res["text"] == "[es] translated", res
    recs = prompt_tap.read_records()
    assert recs, "the DEFAULT PromptTapSink must still write the record to the tap file"
    ok = [r for r in recs if r["outcome"] == "ok"]
    assert ok, ("a successful call must be captured ok", [r["outcome"] for r in recs])
    assert ok[0]["request"]["prompt"] == "hello default", ok[0]["request"]
    # Byte-neutral additive field: a cloud provider carries NO local_path.
    assert "local_path" not in ok[0], "a cloud provider record carries no local_path"
    print(
        "  (G) default path: with no override the record lands in the tap file exactly "
        "as before (local_path absent for a cloud provider) OK"
    )


def main():
    test_noop_sink_suppresses_the_write()
    test_raising_sink_never_breaks_run_completion()
    test_port_level_redact_scrubs_before_the_sink()
    test_redact_is_identity_preserving_and_idempotent()
    test_import_providers_does_not_construct_the_engine()
    test_local_path_derivation_helper()
    test_local_path_lands_in_the_record()
    test_telemetrysink_is_a_structural_check()
    test_default_path_still_writes_the_record()
    print("TELEMETRY SINK OK")


if __name__ == "__main__":
    main()
