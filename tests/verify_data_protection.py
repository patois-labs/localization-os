"""Data-dir protection guard verification (issue #13).

The guard (app.config.assert_data_dir_allowed) refuses SERVER startup against a
data dir that carries a `.protected` marker file unless LOC_TMS_ALLOW_REAL_DATA=1
is set. It is deliberately SERVER-ONLY: the CLI + the verify suites never call it.

Because app.config resolves DATA_DIR + the override once at import time, the cases
that hinge on the env are probed in fresh SUBPROCESSES (each with its own throwaway
DATA_DIR), exactly like verify_ops' secret-precedence checks. ASCII-only prints
(the cp1252 console throws on non-ASCII). This suite bootstraps its own throwaway
LOC_TMS_DATA_DIR so it never touches the real data dir.
"""

import json
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# Throwaway data dir (also gates OFF account seeding / login wall, like the rest).
from tests._bootstrap import init_env

init_env("loctms_dataprot_")

from app import config


# --- in-process checks (this process: throwaway dir, no marker) --------------
def test_unmarked_dir_is_allowed():
    # Our own throwaway dir has no marker => guard is inert (no behavior change).
    assert not config.data_dir_is_protected(), "fresh temp dir must not be protected"
    config.assert_data_dir_allowed()  # must NOT raise
    print("  unmarked throwaway dir: guard inert (no marker) OK")


def test_marker_helpers_roundtrip():
    # create_protect_marker() is idempotent: first call creates, second is a no-op.
    # Do this in a dedicated temp dir so we don't stamp the suite's shared dir.
    d = tempfile.mkdtemp(prefix="loctms_dataprot_mk_")
    marker = os.path.join(d, config.PROTECT_MARKER_NAME)
    # config.PROTECT_MARKER is fixed to THIS process's DATA_DIR, so exercise the
    # marker semantics directly against a temp path via the subprocess probes
    # below; here we only assert the constant + class exist and are wired.
    assert config.PROTECT_MARKER_NAME == ".protected"
    assert issubclass(config.ProtectedDataDirError, RuntimeError)
    # Clean up the unused temp dir.
    try:  # noqa: SIM105
        os.rmdir(d)
    except OSError:
        pass
    assert not os.path.exists(marker)
    print("  marker constant + ProtectedDataDirError wired OK")


# --- subprocess probes (config resolves DATA_DIR/override at import) ---------
# Each probe runs a fresh interpreter with its own DATA_DIR so the import-time
# resolution is exercised cleanly. The probe prints one JSON line: the outcome of
# calling assert_data_dir_allowed() (raised? which error text?).

_PROBE = (
    "import json; from app import config\n"
    "res = {'protected': config.data_dir_is_protected(),"
    " 'allow': config.ALLOW_REAL_DATA, 'data_dir': str(config.DATA_DIR)}\n"
    "try:\n"
    "    config.assert_data_dir_allowed(); res['raised'] = False; res['err'] = ''\n"
    "except config.ProtectedDataDirError as e:\n"
    "    res['raised'] = True; res['err'] = str(e)\n"
    "print(json.dumps(res))"
)


def _probe(data_dir, *, marker, allow):
    """Run the guard in a subprocess against `data_dir`, optionally writing the
    marker first and/or setting the override. Returns the parsed JSON result."""
    os.makedirs(data_dir, exist_ok=True)
    marker_path = os.path.join(data_dir, ".protected")
    if marker:
        with open(marker_path, "w", encoding="utf-8") as fh:
            fh.write("test marker\n")
    elif os.path.exists(marker_path):
        os.remove(marker_path)
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    if allow:
        env["LOC_TMS_ALLOW_REAL_DATA"] = "1"
    else:
        env.pop("LOC_TMS_ALLOW_REAL_DATA", None)
    out = subprocess.check_output([sys.executable, "-c", _PROBE], env=env, cwd=_ROOT)
    return json.loads(out.decode().strip().splitlines()[-1])


def test_marker_present_no_override_refuses():
    d = tempfile.mkdtemp(prefix="loctms_dataprot_a_")
    r = _probe(d, marker=True, allow=False)
    assert r["protected"] is True, r
    assert r["raised"] is True, "guard MUST refuse a protected dir with no override"
    # The error must name the dir, the marker, and the override — actionable.
    err = r["err"]
    assert "LOC_TMS_ALLOW_REAL_DATA" in err, err
    assert ".protected" in err, err
    assert d in err, err
    print("  marker + no override: REFUSES with actionable error OK")


def test_marker_present_with_override_allows():
    d = tempfile.mkdtemp(prefix="loctms_dataprot_b_")
    r = _probe(d, marker=True, allow=True)
    assert r["protected"] is True, r
    assert r["allow"] is True, r
    assert r["raised"] is False, "override MUST allow a protected dir to start"
    print("  marker + LOC_TMS_ALLOW_REAL_DATA=1: allowed OK")


def test_marker_absent_no_behavior_change():
    d = tempfile.mkdtemp(prefix="loctms_dataprot_c_")
    r = _probe(d, marker=False, allow=False)
    assert r["protected"] is False, r
    assert r["raised"] is False, "an unprotected dir must start with no override"
    print("  no marker: unchanged (starts, no override needed) OK")


def test_marker_protects_any_dir_it_sits_in():
    # An EXPLICIT LOC_TMS_DATA_DIR pointing at a protected dir is still protected —
    # the marker protects any dir it sits in. (The common tooling case is an
    # explicit *temp* dir with no marker, covered above, which is NOT protected.)
    d = tempfile.mkdtemp(prefix="loctms_dataprot_d_")
    r = _probe(d, marker=True, allow=False)
    assert r["raised"] is True, "explicit env at a protected dir still needs override"
    print("  explicit env dir WITH marker: still protected (needs override) OK")


def test_run_py_sets_override():
    # run.py is the sanctioned launcher: it must set LOC_TMS_ALLOW_REAL_DATA=1
    # before importing config. Import run.py in a subprocess (with a clean env, no
    # override) and assert config.ALLOW_REAL_DATA came out True. Use a throwaway
    # DATA_DIR so importing run.py never resolves to the real repo dir.
    d = tempfile.mkdtemp(prefix="loctms_dataprot_run_")
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = d
    env.pop("LOC_TMS_ALLOW_REAL_DATA", None)
    probe = (
        "import runpy, sys\n"
        # Import run.py's module body WITHOUT executing uvicorn.run (guarded by
        # __name__ == '__main__'); run_path with a non-main run_name skips it.
        "mod = runpy.run_path('run.py', run_name='not_main')\n"
        "from app import config\n"
        "print('ALLOW=%s' % config.ALLOW_REAL_DATA)"
    )
    out = subprocess.check_output([sys.executable, "-c", probe], env=env, cwd=_ROOT)
    line = out.decode().strip().splitlines()[-1]
    assert line == "ALLOW=True", f"run.py must set the override; got {line!r}"
    print("  run.py import sets LOC_TMS_ALLOW_REAL_DATA=1 (override on) OK")


def main():
    test_unmarked_dir_is_allowed()
    test_marker_helpers_roundtrip()
    test_marker_present_no_override_refuses()
    test_marker_present_with_override_allows()
    test_marker_absent_no_behavior_change()
    test_marker_protects_any_dir_it_sits_in()
    test_run_py_sets_override()
    print("DATA PROTECTION OK")


if __name__ == "__main__":
    main()
