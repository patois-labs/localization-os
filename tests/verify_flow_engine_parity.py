"""Parity harness for the ConnectorTranslateFlow engine extraction (Finding 1).

Behavior-identical bar: the four T2 flows (zendesk / document360 / freshdesk /
zoho) were refactored from four hand-written near-copies into thin wrappers over
``app/services/connectors/flow.run_translate_flow``. A dropped guard in that
refactor is SILENT — the rest of the suite passing does not prove byte-equality of
the emitted results — so this harness pins it down directly.

HOW IT WORKS
------------
``tests/_flow_parity_fixtures.py`` defines a set of scenarios, each driving one
flow against an in-memory Fake tenant (httpx.MockTransport) and capturing the
run's (ok, msg, result-dict, SyncLog rows, persisted hash/watermark config state).
The scenarios exercise every invariant the engine now owns: the WriteBudget
ceiling trim, the FINDING-1 watermark clamp, the incremental-export union, the
content-hash + stale-flag suppression paths, the per-pair write-failure /
watermark-hold path, locale rejection (all-rejected FAIL), empty-body /
unpublished-master skips, the plan-gate ok-semantics branch, and the provenance
actor fold.

The golden expectations in ``tests/flow_engine_goldens.json`` were captured ONCE
from the PRE-refactor store.py by ``tests/capture_flow_goldens.py`` (see that
file's header). This suite re-runs the SAME scenarios against the POST-refactor
engine and asserts each record is byte-equal to its golden. Any divergence is a
behavior change and fails the run.

ASCII-only prints; final line is 'verify_flow_engine_parity OK'.
"""

import json
import os
import sys

# Standard hermetic bootstrap (like every other verify suite). Without it this suite
# only ran green UNDER test_verify_suites.py, whose subprocess env sets LOC_TMS_MOCK_
# FALLBACK; run DIRECTLY it hit the keyless-claude ConfiguredProviderUnavailable error
# (the _flow_parity_fixtures preamble blanks keys + sets a data dir but NOT the mock-
# fallback opt-in). init_env supplies all of it. It runs BEFORE importing the fixtures
# module, whose own os.environ.setdefault(LOC_TMS_DATA_DIR, ...) then keeps this dir.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)  # for tests._bootstrap
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # for _flow_parity_fixtures
from tests._bootstrap import init_env

init_env("loctms_flowparity_")

import _flow_parity_fixtures as fx

_GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_engine_goldens.json")


def _norm(obj):
    """Canonical JSON string for order-insensitive, byte-exact comparison."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True)


def main() -> None:
    with open(_GOLDEN, encoding="ascii") as f:
        goldens = json.load(f)

    records = fx.run_all()

    # Same set of scenarios must be present on both sides.
    missing = sorted(set(goldens) - set(records))
    extra = sorted(set(records) - set(goldens))
    assert not missing, f"scenarios missing from this run: {missing}"
    assert not extra, f"scenarios not in the goldens (regenerate goldens?): {extra}"
    print(f"  {len(records)} scenarios present on both sides")

    mismatches = []
    for name in sorted(goldens):
        want = goldens[name]
        got = records[name]
        for field in ("ok", "msg", "result", "synclog", "config"):
            if _norm(want.get(field)) != _norm(got.get(field)):
                mismatches.append((name, field, want.get(field), got.get(field)))

    if mismatches:
        print("  PARITY MISMATCHES:")
        for name, field, want, got in mismatches[:20]:
            print(f"    [{name}].{field}")
            print(f"      old: {_norm(want)[:300]}")
            print(f"      new: {_norm(got)[:300]}")
        raise AssertionError(f"{len(mismatches)} field(s) diverged from the pre-refactor goldens")

    # Spot-confirm the invariants really were exercised (not vacuously equal).
    zc = records["zendesk::zd_ceiling_clamp"]
    assert zc["result"]["ceiling_hit"] is True, zc
    assert zc["config"]["zendesk_watermark"] == 1782864000, ("FINDING-1 clamp", zc["config"])
    wf = records["zendesk::zd_write_failure"]
    assert wf["config"]["zendesk_watermark"] == 0 and wf["result"]["failures"], wf
    fg = records["freshdesk::fd_plan_gate"]
    assert fg["ok"] is False and fg["result"]["failures"], fg
    print("  invariants exercised: ceiling+watermark clamp, write-fail hold, plan-gate ok=False")

    print(f"  all {len(records)} scenarios byte-equal to the pre-refactor goldens")
    print("verify_flow_engine_parity OK")


if __name__ == "__main__":
    main()
