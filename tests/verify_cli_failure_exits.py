"""CLI failure-exit correctness (#527): a failed operation must never look green.

Each pin drives the real CLI (or the shared remote result-line helper) with the failing
outcome forced through a mock, and asserts the EXIT CODE plus the ok flag in the --json
payload. Offline: providers stay mocked, nothing touches the network or a paid engine.

  1. (retired with the Databricks export)
  2. Remote `export run`: a PURGED or EXPIRED run is terminal (exit 1), not polled for 30 min.
  3. `lqa qe-compare` / `jev-refine` with --remote: a usage refusal (exit 2), recorded nowhere
     as a quiet "error"; `--json` pointing at the --pairs file is refused and the input is
     left byte-identical.
  4. `enterprise preflight`: a RED preflight exits 1 (the --json payload keeps the checks).
  5. No traceback: `vendor rate-card set` with a negative or nan rate exits 2 cleanly, and
     `export status` with a Unicode-digit ref is a clean "no such export run" (exit 2).

Final line: "CLI FAILURE EXITS OK".
"""

import json
import os
import sys
import tempfile
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cli_failexit_")

from app.db import init_db
from tests._cli_runner import run_cli


def _last_json(out):
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else {}


class _FakeClient:
    def __init__(self, polls):
        self._polls = list(polls)
        self.gets = 0

    def post(self, path, json=None):
        return {"run_uuid": "r-1", "status": "queued"}

    def get(self, path):
        self.gets += 1
        return self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]


def test_remote_export_run_terminal_states():
    import time

    from app.cli import _remote_impl_part7 as p7
    from app.cli._common import Emitter

    real_client, real_sleep = p7._client, time.sleep
    time.sleep = lambda s: (_ for _ in ()).throw(AssertionError("polled a terminal run"))
    try:
        for status in ("purged", "expired", "failed"):
            fake = _FakeClient([{"run_uuid": "r-1", "status": status}])
            p7._client = lambda args, f=fake: f
            args = SimpleNamespace(
                scope="all", history="current", ref=None, emitters=None, audit=False
            )
            rc = p7.export_run(args, Emitter(quiet=True))
            assert rc == 1, (status, rc)
            assert fake.gets == 1, (status, fake.gets)
        fake = _FakeClient([{"run_uuid": "r-1", "status": "ok", "artifact_bytes": 3}])
        p7._client = lambda args, f=fake: f
        assert p7.export_run(args, Emitter(quiet=True)) == 0
    finally:
        p7._client, time.sleep = real_client, real_sleep
    print("  2. remote export run: purged/expired are terminal (exit 1), ok still 0")


def test_lqa_remote_refusal_and_report_clobber():
    tmp = tempfile.mkdtemp(prefix="loctms_failexit_")
    pairs = os.path.join(tmp, "pairs.jsonl")
    body = (
        json.dumps({"source": "Hi", "target": "Hola", "source_locale": "en", "target_locale": "es"})
        + "\n"
    )
    with open(pairs, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    remote = ["--remote", "http://127.0.0.1:9", "--token", "lsu_" + "x" * 8]
    for verb in (["qe-compare", "--pairs", pairs], ["jev-refine", "--pairs", pairs]):
        code, out, err = run_cli([*remote, "lqa", *verb])
        assert code == 2, (verb, code, out, err)
        assert "in-process" in (out + err), (out, err)
    # --json <path> equal to --pairs would overwrite the input: refused before any work.
    for verb in ("qe-compare", "jev-refine"):
        code, out, err = run_cli(
            ["lqa", verb, "--pairs", pairs, "--json", pairs, "--llm-provider", "mock"]
            if verb == "qe-compare"
            else ["lqa", verb, "--pairs", pairs, "--json", pairs, "--provider", "mock"]
        )
        assert code == 2, (verb, code, out, err)
        with open(pairs, encoding="utf-8", newline="") as fh:
            assert fh.read() == body, f"{verb} clobbered the --pairs input"
    print("  3. lqa --remote refusal exits 2; --json == --pairs refused, input untouched")


def test_enterprise_preflight_red_exits_1():
    from app.cli import cmd_enterprise
    from app.services import enterprise_enablement as ee

    real = ee.preflight
    check = SimpleNamespace(name="x", ok=False, detail="boom", network=False)
    try:
        for ok, want in ((False, 1), (True, 0)):
            ee.preflight = lambda feature, run_network=False, ok=ok: SimpleNamespace(
                feature=feature, ok=ok, network_ran=True, checks=[check]
            )
            code, out, err = run_cli(["--json", "enterprise", "preflight", "saml"])
            assert code == want, (ok, code, out, err)
            payload = _last_json(out)
            assert payload["preflight_ok"] is ok and payload["ok"] is ok, payload
            assert payload["checks"][0]["name"] == "x", payload
            code, out, err = run_cli(["enterprise", "preflight", "saml"])
            assert code == want, (ok, code, out, err)
    finally:
        ee.preflight = real
    assert cmd_enterprise  # imported for the registration side effect
    print("  4. enterprise preflight: RED -> exit 1, GREEN -> 0, checks kept in --json")


def test_clean_errors_not_tracebacks():
    code, out, err = run_cli(
        ["vendor", "add", "RateVendor", "--endpoint-url", "https://v.example.com/intake"]
    )
    assert code == 0, (code, out, err)
    code, out, err = run_cli(["--json", "vendor", "list"])
    vid = _last_json(out)["vendors"][0]["id"]
    for rate in ("-1", "nan", "inf"):
        code, out, err = run_cli(
            ["vendor", "rate-card", "set", str(vid), "--band", "new", "--rate=" + rate]
        )
        assert code == 2, (rate, code, out, err)
        assert "Traceback" not in (out + err), (rate, out, err)
    code, out, err = run_cli(
        ["vendor", "rate-card", "set", str(vid), "--band", "new", "--rate", "0.12"]
    )
    assert code == 0, (code, out, err)
    # A superscript digit (U+00B2) passes str.isdigit() but int() rejects it: it must be a
    # clean "no such export run" usage error, not a ValueError traceback.
    for ref in ("²", "²³"):
        code, out, err = run_cli(["export", "status", ref])
        assert code == 2, (ref, code, out, err)
        assert "no such export run" in (out + err), (ref, out, err)
    print("  5. vendor rate-card set bad rate + unicode-digit export ref: clean exit 2")


def main_() -> None:
    init_db()
    test_remote_export_run_terminal_states()
    test_lqa_remote_refusal_and_report_clobber()
    test_enterprise_preflight_red_exits_1()
    test_clean_errors_not_tracebacks()
    print("CLI FAILURE EXITS OK")


if __name__ == "__main__":
    main_()
