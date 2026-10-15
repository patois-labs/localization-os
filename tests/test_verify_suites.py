"""Pytest entrypoint over the bespoke verify_*.py suites (Phase 4-D).

Each ``tests/verify_*.py`` is a STANDALONE script that, at IMPORT time, sets its
own ``os.environ["LOC_TMS_DATA_DIR"] = tempfile.mkdtemp(...)`` and then imports
``app`` (module-cached). Because the data dir is chosen at import and ``app`` is
cached per-process, two suites CANNOT share one interpreter — they would collide
on the already-imported ``app``/engine. So each suite must run as its OWN
subprocess.

This module does exactly that: it globs the suites, parametrizes one test case
per script (id = the suite name), runs each via ``sys.executable <path>`` in a
child process, and asserts:

  * the child exits 0, AND
  * the last non-empty stdout line ends with ``OK`` (every suite prints
    ``... OK`` on success).

This makes plain ``pytest`` run the whole suite cross-platform (Linux CI + local
Windows) with a single command, WITHOUT importing the verify scripts into the
pytest process (which would trigger their import-time data-dir setup + app import
and collide). pytest is configured (pyproject.toml) to collect only ``test_*.py``,
so it never tries to import the ``verify_*.py`` scripts as test modules — this
wrapper is the only collected module, plus its N parametrized cases.

The verify scripts themselves are NOT modified: each one still runs standalone as
``python tests/verify_<name>.py``.

CI sharding: set ``LOC_TMS_SUITE_SHARD=i/n`` (for example ``3/8``) to run only the i-th
of n deterministic shards of the suites (see tests/_suite_shard.py). ``-k`` still applies
on top of the shard.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _TESTS_DIR)

from _suite_shard import select_shard

# --- Harness reliability (train-harness-reliability) ------------------------
#
# Two pre-existing harness bugs made the tail of the ~38-minute full run
# unreliable:
#
#   * DECODE: subprocess.run(text=True) decodes the child's pipes with the
#     PARENT's locale (cp1252 on Windows), even though PYTHONIOENCODING=utf-8 is
#     set in the CHILD env. A single stray non-ASCII byte (e.g. 0x90) emitted by
#     a suite (or a grandchild it spawns) then raised UnicodeDecodeError inside
#     communicate()'s reader thread and killed the run mid-suite. Fixed by
#     decoding as utf-8 with errors="replace" on the capturing call below.
#
#   * TEMP LEAK: every verify suite mkdtemp()s a loctms*-prefixed DATA_DIR (via
#     tests/_bootstrap.init_env) and a few mkdtemp() other dirs directly
#     (verify_alembic_authority), plus child suites (verify_migrations,
#     verify_mcp) spawn their own processes that mkdtemp() again. _bootstrap
#     registers an atexit rmtree, but on Windows that races the still-open
#     SQLite handle inside the SAME dying process, so ignore_errors leaves the
#     locked dir behind — tens of thousands accumulated in %TEMP%. We fix it at
#     the harness level instead: each child is pointed at a private, run-scoped
#     temp root (TMP/TEMP/TMPDIR), and we rmtree that root AFTER the child has
#     fully exited (all handles released), which covers init_env, direct
#     mkdtemp callsites, AND grandchild-spawned dirs in one place.

# Temp dirs older than this at harness start are stale debris from prior runs
# and are purged once. The age guard is MANDATORY: other test processes (e.g. a
# concurrent campaign train) may be running on this box right now, and their
# fresh dirs must never be touched.
_STALE_MAX_AGE_S = 24 * 60 * 60

# Both prefixes this harness (and the suites it drives) create in the system
# temp: loctms* = per-suite DATA_DIRs mkdtemp'd by _bootstrap.init_env and by a
# handful of suites directly; loc_harness* = the per-suite temp ROOTS this
# harness creates for each child (normally rmtree'd in the finally below, but a
# parent killed WITHOUT running that finally — hard kill, reboot, worktree
# pruned mid-run — strands one, and each holds a whole suite's tree). Purging
# both here is the only thing that ever reaps a stranded root.
_STALE_PREFIXES = ("loctms*", "loc_harness*")


def _purge_stale_temp_dirs():
    """One-shot: delete this harness's stale temp dirs (both prefixes) in the
    system temp that are OLDER than 24h. Age-guarded so concurrent runs' fresh
    dirs (including live loc_harness roots) are never removed. Best-effort;
    logs the count purged to stderr."""
    root = tempfile.gettempdir()
    now = time.time()
    purged = 0
    for pattern in _STALE_PREFIXES:
        for path in glob.glob(os.path.join(root, pattern)):
            try:
                if not os.path.isdir(path):
                    continue
                age = now - os.path.getmtime(path)
                if age < _STALE_MAX_AGE_S:
                    continue  # fresh — may belong to a concurrent run; leave it
                shutil.rmtree(path, ignore_errors=True)
                if not os.path.exists(path):
                    purged += 1
            except OSError:
                continue
    print(
        f"[test_verify_suites] purged {purged} stale temp dir(s) "
        f"(loctms*/loc_harness*, >24h old) from {root}",
        file=sys.stderr,
    )
    return purged


# Runs once per pytest process, at collection time.
_purge_stale_temp_dirs()

# One subprocess per script; generous per-suite timeout. This is a SHARED cap
# across every verify_*.py, sized for the genuinely slow suites — verify_migrations
# and verify_mcp spawn their own child processes / uvicorn threads. Kept well clear
# of any single suite's real runtime so a 2-core CI runner never trips it.
#
# NOTE: verify_cli used to be the binding constraint at ~143s locally. That was NOT
# subprocess overhead (it already runs commands in-process — only `init` uses a
# subprocess, by necessity): the cost was `tm import` under semantic-TM-enabled
# auto-embedding falling through to the LM Studio 'local' embedder and blocking on
# a ~65s connection timeout per import (settings.apply_to_config re-injected the
# hardcoded local_base_url default that the env-clear had blanked). Pinning the
# test to the offline MockEmbedder cut verify_cli to ~12s. The 600s cap now exists
# purely for the child-process suites; it is kept (not lowered to 300) so those,
# not verify_cli, define the margin — on a 2-core runner they still finish well
# under it, with room to spare.
_SUITE_TIMEOUT_S = 600

# Out-of-tree bytecode cache for children when the caller disabled bytecode writing (see
# test_verify_suite). Not a loctms*/loc_harness* name, so the stale-temp purge leaves it.
_PYCACHE_PREFIX = os.path.join(tempfile.gettempdir(), "locos_pycache")


def _discover_suites():
    """Every tests/verify_*.py, sorted, as (suite_name, absolute_path) pairs."""
    names = sorted(
        f for f in os.listdir(_TESTS_DIR) if f.startswith("verify_") and f.endswith(".py")
    )
    return [(os.path.splitext(n)[0], os.path.join(_TESTS_DIR, n)) for n in names]


_SUITES = _discover_suites()
# Registration guard for the offline reviewer-comparison suite (glob-discovered).
assert "verify_glossary_paging_scale" in {name for name, _path in _SUITES}
assert "verify_key_paging_scale" in {name for name, _path in _SUITES}
assert "verify_srp_move_check" in {name for name, _path in _SUITES}
assert "verify_srp_split" in {name for name, _path in _SUITES}
assert "verify_review_diff" in {name for name, _path in _SUITES}
assert "verify_embedded_host_example" in {name for name, _path in _SUITES}
assert "verify_pii_all_paths" in {name for name, _path in _SUITES}
assert "verify_pii_feedback_refusal" in {name for name, _path in _SUITES}
assert "verify_image_scanning" in {name for name, _path in _SUITES}
_SUITES = select_shard(_SUITES, os.environ.get("LOC_TMS_SUITE_SHARD"))


@pytest.mark.parametrize(
    "suite_path",
    [path for _name, path in _SUITES],
    ids=[name for name, _path in _SUITES],
)
def test_verify_suite(suite_path):
    """Run one verify_*.py script as a subprocess; require rc==0 and a final 'OK'."""
    # Defense-in-depth: each verify_*.py already blanks these itself right after
    # setting LOC_TMS_DATA_DIR (before importing app/config), so the suite is
    # hermetic on its own. This strips them from the subprocess env too, so a
    # suite that somehow missed the in-file clear still can't fall through to a
    # live provider because the owner's .env has a real key (config.py uses
    # load_dotenv(override=False), so a blank/unset env var here is required —
    # merely relying on the child re-reading .env would not help).
    env = os.environ.copy()
    env["LOC_TMS_TEST_MODE"] = "1"
    for key in (
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "TYPESAFE_API_KEY",
        "LOCAL_BASE_URL",
    ):
        env[key] = ""
    # The verify suites are OFFLINE BY DESIGN: every key is blanked above, yet scores of
    # fixtures pin default_provider="claude" (etc.) and rely on the keyless built-in
    # resolving to the offline MockProvider. That fallback is now REFUSED on a configured
    # install (providers.get_provider raises ConfiguredProviderUnavailable so a keyless
    # prod install can never fake output — the 2026-08-07 incident). This flag is the
    # explicit "this process is offline, mock is expected" opt-in that keeps the hermetic
    # suite on its historical posture; it is DEFAULT-OFF and MUST stay unset in production.
    # (verify_mock_fallback_loud.py toggles config.MOCK_FALLBACK_OK off per-case to prove
    # the loud refusal.)
    env["LOC_TMS_MOCK_FALLBACK"] = "1"
    # Force UTF-8 stdout in the child (B1): on a Windows box the subprocess pipe reports
    # cp1252, so a suite that prints any non-ASCII char (e.g. the ⟦⟧ inline-token glyphs)
    # dies with UnicodeEncodeError under this harness even though it passes in a UTF-8
    # shell. Systemic: any current or future verify_*.py may print Unicode. (Each suite is
    # ALSO made ASCII-safe in its own prints as defense in depth.)
    env["PYTHONIOENCODING"] = "utf-8"
    # BYTECODE CACHE (test speed, 2026-10-08): agent shells and scripts/pregate.py export
    # PYTHONDONTWRITEBYTECODE=1 so a clone stays free of __pycache__. Under it every suite
    # child recompiled all ~600 app modules from source, about 2.5 s per process on the
    # Windows box (more for suites that spawn their own children). Keep the clone clean the
    # other way: let the children write bytecode, but into a cache OUTSIDE the tree
    # (PYTHONPYCACHEPREFIX, shared by every run on this machine; Python checks each entry
    # against its source's mtime and size, so an edit is never served stale). Without the
    # variable nothing changes: bytecode goes to __pycache__ as it always did.
    if env.get("PYTHONDONTWRITEBYTECODE") and not env.get("PYTHONPYCACHEPREFIX"):
        env.pop("PYTHONDONTWRITEBYTECODE")
        env["PYTHONPYCACHEPREFIX"] = _PYCACHE_PREFIX

    # Point the child (and any grandchildren it spawns) at a private, run-scoped
    # temp root so every loctms* DATA_DIR mkdtemp'd by _bootstrap.init_env — plus
    # the direct mkdtemp()s in suites like verify_alembic_authority and the temp
    # dirs of child processes spawned by verify_migrations/verify_mcp — lands
    # inside it instead of the shared system temp. We rmtree it AFTER the child
    # exits (below), when all its file handles are released, so Windows SQLite
    # locks that defeat the in-child atexit cleanup no longer strand dirs. All
    # three spellings are set for cross-platform coverage (TMPDIR: POSIX/Linux
    # CI; TEMP/TMP: Windows). tempfile.gettempdir() reads these at child startup.
    suite_tmp = tempfile.mkdtemp(prefix="loc_harness_")
    env["TMPDIR"] = suite_tmp
    env["TEMP"] = suite_tmp
    env["TMP"] = suite_tmp

    try:
        proc = subprocess.run(
            [sys.executable, suite_path],
            cwd=os.path.dirname(_TESTS_DIR),  # repo root, matching the legacy loop
            capture_output=True,
            text=True,
            # DECODE FIX: force utf-8 decoding of the child's pipes regardless of
            # the parent's locale (cp1252 on Windows). errors="replace" keeps a
            # stray non-ASCII byte from raising UnicodeDecodeError in
            # communicate()'s reader thread and killing the run.
            encoding="utf-8",
            errors="replace",
            timeout=_SUITE_TIMEOUT_S,
            env=env,
        )
    finally:
        # After the child (and its own children) have exited, the run-scoped
        # temp root can be removed cleanly — no live handles remain.
        shutil.rmtree(suite_tmp, ignore_errors=True)

    # A useful tail of combined output for failure diagnosis.
    combined = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    tail = "\n".join(combined.splitlines()[-40:])

    assert proc.returncode == 0, (
        f"{os.path.basename(suite_path)} exited {proc.returncode}\n--- output tail ---\n{tail}"
    )

    stdout_lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    # Surface any suite's own case inventory on SUCCESSFUL runs too (pytest -rP), not just
    # on failure. A suite that skips silently otherwise reads as a pass: verify_ldap exits 0
    # with a SKIP when the opt-in ldap3 extra is absent, which is exactly how a green run can
    # hide that its cases never executed. Generic on purpose -- any suite that prints a
    # "collected ids:" line gets the same treatment, rather than special-casing one suite
    # inside the shared runner.
    for line in stdout_lines:
        if "collected ids:" in line:
            print(line)
    assert stdout_lines, (
        f"{os.path.basename(suite_path)} produced no stdout\n--- output tail ---\n{tail}"
    )
    last = stdout_lines[-1].strip()
    assert last.endswith("OK"), (
        f"{os.path.basename(suite_path)} did not finish with an 'OK' line "
        f"(last line: {last!r})\n--- output tail ---\n{tail}"
    )
