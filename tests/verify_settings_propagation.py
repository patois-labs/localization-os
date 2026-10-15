"""Settings cache — CROSS-PROCESS propagation (audit K3) and the hot-path contract.

The finding, reproduced in live QA on 2026-08-03: `app/services/settings.py` kept a
read-through `_cache` that was coherent only for writes made in THIS process ("All
writes go through set_many() in this process, so the cache stays coherent"). Setting
`key_projects_enabled=0` from the CLI (a separate process) left the RUNNING web server
serving every key route with 200 while the CLI itself correctly refused; only a restart
made them 404. It affects EVERY setting, but it matters most for a switch documented as
emergency-off — on a hosted platform, a remote shell + CLI could not stop a feature at all.

What this suite pins:

  A  CROSS-PROCESS: a SECOND PROCESS writes a setting; this process's settings.get()
     serves the new value within CHECK_INTERVAL_SECONDS, with no restart and no
     invalidate_cache() call. THE REGRESSION TEST — it fails against the pre-fix file.
  B  THE K3 SHAPE END TO END: a second process flips `key_projects_enabled` to "0" and
     this process's LIVE web app 404s the key grid it was serving 200 a moment ago
     (and `keys.enabled()` agrees), then comes back when the switch is flipped back.
  C  HOT PATH: the cache still absorbs repeated reads. A warm get() issues ZERO
     `setting` SELECTs within the interval, and a long burst of reads costs at most
     one probe per interval — never one read per get().
  D  NO REGRESSION: an in-process set_many() is visible IMMEDIATELY (not after a TTL),
     including through apply_to_config(), and invalidate_cache()/forget() still work.
  E  THREAD SAFETY: concurrent readers + a concurrent writer + expiring probes never
     raise, never deadlock, and never serve a value that was never set.
  F  The version row is INTERNAL: not in DEFAULTS, not in all_values(), not settable
     through set_many(), self-healing if poked, and no setting's default or semantics
     moved. bump_version() (F5) is the one deliberate way to move it without writing
     any value -- the operator's recovery affordance after an out-of-band write.

The rest defend the MECHANISMS the design rests on — added after a review found that
7 of 10 mutations to settings.py survived the sections above (a passing suite that does
not pin the machinery is not evidence the machinery works):

  G  THE `_generation` GUARD: a cache MISS that started before a cross-process change
     must never write its stale reading back AFTER the invalidating clear. Without the
     guard the process's _version already matches the DB, so NO future probe ever
     corrects it -- permanently stale, which is audit K3 itself.
  H  ONE TRANSACTION: the version bump commits with the values or not at all, on ONE
     connection, with exactly ONE COMMIT, the bump inside it.
  I  A PROBE NEVER HALF-APPLIES: even on the "no prior version reading" path (process
     start / after invalidate_cache) it pushes into the `config` module, so the value
     cache and config.DEFAULT_PROVIDER can never disagree.
  J  A NO-OP SAVE IS FREE FOR EVERYONE ELSE: an empty dict, a dict that filters to
     empty, and a value re-saved identical to the one on disk write nothing and bump
     nothing -- while a real change still bumps exactly once.

Hermetic: throwaway LOC_TMS_DATA_DIR (shared with the child processes on purpose —
that IS the two-process shape), cleared provider keys, ASCII-only prints.
"""

import logging
import os
import re
import subprocess
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_setprop_")

from sqlalchemy import event
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import Setting
from app.services import settings

init_db()
settings.apply_to_config()

# The real sqlmodel.Session, kept so the G/H sections can swap settings.Session for a
# wrapper and put it back. Captured once, before anything patches anything.
_REAL_SESSION = settings.Session

# Short enough that a suite does not sit in sleep(), long enough to be a real window.
_TTL = 0.30
settings.CHECK_INTERVAL_SECONDS = _TTL


# --- the second process -------------------------------------------------------
# A real child interpreter, pointed at the SAME data dir, that boots the app the way
# the CLI does and saves through the ordinary service seam. Nothing about the parent's
# in-memory state is shared with it — which is the whole point of the finding.
_CHILD = r"""
import os, sys
sys.path.insert(0, %(root)r)
os.chdir(%(root)r)
os.environ["LOC_TMS_DATA_DIR"] = %(tmp)r
for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_BASE_URL"):
    os.environ[_k] = ""
from app.db import init_db
init_db()
from app.services import settings
settings.set_many(%(items)r)
print("child-wrote")
"""


def _write_from_another_process(items: dict) -> None:
    """Save `items` from a SEPARATE interpreter against the same data dir."""
    src = _CHILD % {"root": _ROOT, "tmp": _TMP, "items": items}
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = _TMP
    proc = subprocess.run(
        [sys.executable, "-c", src], capture_output=True, text=True, env=env, timeout=180
    )
    assert proc.returncode == 0, (
        "the writer process failed",
        proc.returncode,
        proc.stdout,
        proc.stderr[-2000:],
    )
    assert "child-wrote" in proc.stdout, proc.stdout


# The same shape, but calling the F-R1 escape hatch (settings.bump_version()) instead
# of set_many -- an operator move that writes NO setting value, only the version.
_CHILD_BUMP = r"""
import os, sys
sys.path.insert(0, %(root)r)
os.chdir(%(root)r)
os.environ["LOC_TMS_DATA_DIR"] = %(tmp)r
for _k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_BASE_URL"):
    os.environ[_k] = ""
from app.db import init_db
init_db()
from app.services import settings
settings.bump_version()
print("child-bumped")
"""


def _bump_version_from_another_process() -> None:
    """Call settings.bump_version() from a SEPARATE interpreter against the same data
    dir -- the operator's recovery move after an out-of-band write, with no value of
    its own to save."""
    src = _CHILD_BUMP % {"root": _ROOT, "tmp": _TMP}
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = _TMP
    proc = subprocess.run(
        [sys.executable, "-c", src], capture_output=True, text=True, env=env, timeout=180
    )
    assert proc.returncode == 0, (
        "the bump-only process failed",
        proc.returncode,
        proc.stdout,
        proc.stderr[-2000:],
    )
    assert "child-bumped" in proc.stdout, proc.stdout


def _wait_for(fn, want, timeout=None):
    """Poll `fn` until it equals `want` or the budget runs out; return the elapsed
    seconds (or None on timeout). Polling is the honest shape: the guarantee is
    'within a few seconds', not 'on the next call'."""
    budget = timeout if timeout is not None else _TTL * 8
    started = time.monotonic()
    while time.monotonic() - started < budget:
        if fn() == want:
            return time.monotonic() - started
        time.sleep(_TTL / 6)
    return None


# --- a spy on the DB reads of the setting table -------------------------------
_WRITE_TO_SETTING = re.compile(r'^(insert\s+into|update|delete\s+from)\s+"?setting"?\b')


class _SettingReads:
    """Counts SELECTs against the `setting` table on the app engine. Counting real
    SQL (not a monkeypatched Session) is what makes 'the cache still absorbs reads'
    an honest claim: it catches ANY path back to the DB, including the probe.

    `writes` counts INSERT/UPDATE/DELETE against the same table (section J needs it:
    a no-op save must issue none)."""

    def __init__(self):
        self.selects = 0
        self.writes = 0
        self._on = False

    def _hook(self, conn, cursor, statement, params, ctx, many):
        if not self._on:
            return
        s = " ".join(statement.split()).lower()
        if s.startswith("select") and "from setting" in s:
            self.selects += 1
        elif _WRITE_TO_SETTING.match(s):
            self.writes += 1

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hook)
        self._on = True
        return self

    def __exit__(self, *exc):
        self._on = False
        event.remove(engine, "before_cursor_execute", self._hook)
        return False


class _CapturedWarnings(logging.Handler):
    """Collects WARNING+ messages from the settings logger for the window it wraps."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def __enter__(self):
        logging.getLogger("loc_tms.settings").addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger("loc_tms.settings").removeHandler(self)
        return False


# ============================================================================== #
# A — the regression test: a second process's write reaches this one
# ============================================================================== #
def test_a_an_out_of_process_write_reaches_a_running_process():
    settings.set_many({"org_name": "Before The Handoff"})
    assert settings.get("org_name") == "Before The Handoff"

    _write_from_another_process({"org_name": "Written Elsewhere"})

    took = _wait_for(lambda: settings.get("org_name"), "Written Elsewhere")
    assert took is not None, (
        "a setting written by ANOTHER PROCESS never reached this one -- the cache is "
        "per-process with no cross-process invalidation (audit K3). Still serving: {!r}".format(
            settings.get("org_name")
        )
    )
    assert took <= _TTL * 6, ("propagation took far longer than the check interval", took, _TTL)
    print(
        f"  [A] an out-of-process write is served here in {took:.2f}s (interval {_TTL:.2f}s), "
        "with no restart and no invalidate_cache OK"
    )


def test_a2_propagation_needs_no_restart_and_survives_repeats():
    """Not a one-shot: the process keeps tracking a foreign writer over and over."""
    for n in range(3):
        want = "round-%d" % n  # noqa: UP031
        _write_from_another_process({"org_name": want})
        assert _wait_for(lambda: settings.get("org_name"), want) is not None, (
            "propagation stopped working after the first cross-process pickup",
            n,
        )
    print("  [A2] three consecutive out-of-process writes are each picked up OK")


# ============================================================================== #
# B — the K3 shape end to end: the kill switch kills a RUNNING server
# ============================================================================== #
def test_b_the_kill_switch_stops_a_running_server():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import content
    from app.services import keys as keys_svc

    settings.set_many({"key_projects_enabled": "1"})
    with Session(engine) as s:
        project = content.project_create(
            s,
            name="K3 Kill Switch Probe",
            source_locale="en-US",
            target_locales="de-DE",
            project_type="key_based",
        )
        pid = project.id
    client = TestClient(app)

    r = client.get("/projects/%d/keys" % pid)  # noqa: UP031
    assert r.status_code == 200, ("the key grid must serve before the flip", r.status_code)
    assert keys_svc.enabled() is True

    # THE FINDING: this is the CLI's job in real life, done from a real second process.
    _write_from_another_process({"key_projects_enabled": "0"})

    got = _wait_for(lambda: client.get("/projects/%d/keys" % pid).status_code, 404)  # noqa: UP031
    assert got is not None, (
        "the kill switch could not kill a RUNNING server: the key grid still answers "  # noqa: UP031
        "%d after an out-of-process `config set key_projects_enabled 0` (audit K3)"
        % client.get("/projects/%d/keys" % pid).status_code  # noqa: UP031
    )
    assert keys_svc.enabled() is False, (
        "the route 404s but the service predicate disagrees -- they must read the same live value"
    )

    # ...and back on again, from another process, without a restart either way.
    _write_from_another_process({"key_projects_enabled": "1"})
    assert _wait_for(lambda: client.get("/projects/%d/keys" % pid).status_code, 200) is not None, (  # noqa: UP031
        "the switch does not come back ON live"
    )
    print(
        "  [B] an out-of-process kill-switch flip 404s a RUNNING server's key routes "
        f"in ~{got:.2f}s and restores them the same way OK"
    )


# ============================================================================== #
# C — the hot path: repeated reads are still absorbed by the cache
# ============================================================================== #
def test_c_repeated_reads_within_the_interval_never_touch_the_db():
    settings.get("org_name")  # warm the cache + reset the probe clock
    with _SettingReads() as spy:
        for _ in range(20000):
            settings.get("org_name")
        # 20k dict reads take far less than the interval, so not one of them may go
        # to the database -- neither for the value nor for the version.
        assert spy.selects == 0, (
            "the cache no longer absorbs repeated reads: %d `setting` SELECTs for "  # noqa: UP031
            "20000 gets inside one %.2fs interval" % (spy.selects, _TTL)
        )
    print("  [C] 20000 warm gets inside one interval = 0 setting SELECTs OK")


def test_c2_a_long_burst_costs_at_most_one_probe_per_interval():
    settings.get("org_name")
    window = _TTL * 4
    with _SettingReads() as spy:
        started = time.monotonic()
        reads = 0
        while time.monotonic() - started < window:
            settings.get("org_name")
            reads += 1
    elapsed = time.monotonic() - started
    ceiling = int(elapsed / _TTL) + 2  # +2: boundary rounding + the first probe
    assert spy.selects <= ceiling, (
        "the probe is running more often than once per interval",
        spy.selects,
        ceiling,
        reads,
    )
    assert reads > 1000, ("the burst was too small to be evidence", reads)
    print(
        "  [C2] %d gets over %.2fs cost %d setting SELECTs (ceiling %d) -- the DB "  # noqa: UP031
        "cost is per-INTERVAL, not per-get OK" % (reads, elapsed, spy.selects, ceiling)
    )


def test_c3_a_cache_miss_still_costs_exactly_one_read():
    settings.invalidate_cache()
    settings.get("org_name")  # absorbs the post-invalidate probe
    with _SettingReads() as spy:
        settings.get("smtp_host")  # a key never read in this process yet
        first = spy.selects
        settings.get("smtp_host")
        settings.get("smtp_host")
    assert first <= 2, ("a cold read costs more than the value + one probe", first)
    assert spy.selects == first, (
        "a warm read after a miss went back to the DB",
        spy.selects,
        first,
    )
    print("  [C3] a cold key costs <=2 reads and is warm thereafter OK")


# ============================================================================== #
# D — no regression: in-process writes stay IMMEDIATE
# ============================================================================== #
def test_d_an_in_process_write_is_visible_immediately():
    settings.set_many({"org_name": "Immediate"})
    assert settings.get("org_name") == "Immediate", (
        "an in-process set_many must be visible on the very next get, with no TTL "
        "wait -- the cross-process work must not have made saves eventually-consistent"
    )

    # and through apply_to_config, which pushes into the config module
    settings.set_many({"default_provider": "mock"})
    assert settings.get("default_provider") == "mock"
    assert config.DEFAULT_PROVIDER == "mock", (
        "set_many must still push into the config module",
        config.DEFAULT_PROVIDER,
    )
    settings.set_many({"default_provider": settings.DEFAULTS["default_provider"]})
    print("  [D] an in-process save is visible on the next get, and still applies to config OK")


def test_d2_a_direct_db_poke_plus_invalidate_cache_still_works():
    with Session(engine) as s:
        row = s.get(Setting, "org_name")
        row.value = "Poked Directly"
        s.add(row)
        s.commit()
    settings.invalidate_cache()
    assert settings.get("org_name") == "Poked Directly"
    print("  [D2] invalidate_cache after a direct DB poke still works OK")


def test_d3_forget_still_evicts_without_writing():
    settings.set_many({"key_autounverify:99991": "0"})
    assert settings.get("key_autounverify:99991") == "0"
    with Session(engine) as s:
        s.delete(s.get(Setting, "key_autounverify:99991"))
        s.commit()
    with _SettingReads() as spy:
        settings.forget(["key_autounverify:99991"])
        assert spy.selects == 0, "forget() must stay cache-only (it never writes)"
    assert settings.get("key_autounverify:99991", None) is None, (
        "forget() must stop the cache serving a deleted row's last value"
    )
    print("  [D3] forget() still evicts, still writes nothing OK")


def test_d4_defaults_and_semantics_are_untouched():
    baseline = {
        "key_projects_enabled": "1",
        "image_localization_enabled": "1",
        "audit_enabled": "1",
        "multi_org_enabled": "0",
        "retention_enabled": "0",
        "security_auth_required": "0",
        "mt_concurrency": "4",
    }
    for key, want in baseline.items():
        assert settings.DEFAULTS[key] == want, (
            "a default moved",
            key,
            settings.DEFAULTS[key],
            want,
        )
    print("  [D4] the kill-switch / safety defaults are unchanged OK")


# ============================================================================== #
# E — thread safety
# ============================================================================== #
def test_e_concurrent_readers_and_a_writer_stay_consistent():
    legal = {"thread-A", "thread-B"}
    settings.set_many({"org_name": "thread-A"})
    errors = []
    seen = set()
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                v = settings.get("org_name")
                seen.add(v)
                assert v in legal, ("a value that was never set", v)
                # a second key so the miss path is exercised concurrently too
                settings.get("smtp_from")
        except Exception as exc:
            errors.append(exc)

    def writer():
        try:
            for n in range(40):
                settings.set_many({"org_name": "thread-B" if n % 2 else "thread-A"})
                time.sleep(0.005)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
    w = threading.Thread(target=writer, daemon=True)
    for t in threads:
        t.start()
    w.start()
    w.join(timeout=120)
    stop.set()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a reader thread did not finish (deadlock?)"
    assert not errors, ("concurrent settings access raised", errors[:3])
    assert seen <= legal and seen, ("readers saw something never written", seen)
    settings.set_many({"org_name": "Localization OS"})
    print(
        "  [E] 8 concurrent readers + a writer + expiring probes: no error, no "
        "deadlock, no invented value OK"
    )


def test_e2_a_probe_never_holds_the_lock_across_the_db_read():
    """A probe that held _cache_lock across its DB round trip would serialize every
    hot-path get() behind it. Proven by making the probe's read SLOW and checking a
    reader on another thread still completes promptly."""
    settings.get("org_name")
    real_read = settings._read_version
    gate = threading.Event()

    def slow_read():
        gate.wait(timeout=5)
        return real_read()

    settings._read_version = slow_read
    try:
        time.sleep(_TTL * 1.2)  # make the next get() probe
        prober = threading.Thread(target=lambda: settings.get("org_name"), daemon=True)
        prober.start()
        time.sleep(0.15)  # the probe is now mid-read
        done = threading.Event()
        threading.Thread(target=lambda: (settings.get("org_name"), done.set()), daemon=True).start()
        assert done.wait(timeout=2.0), (
            "a get() blocked behind an in-flight probe -- the probe is holding "
            "_cache_lock across its DB read"
        )
    finally:
        gate.set()
        prober.join(timeout=10)
        settings._read_version = real_read
    print("  [E2] a get() is never blocked behind an in-flight version probe OK")


# ============================================================================== #
# F — the version row is internal machinery, not a setting
# ============================================================================== #
def test_f_the_version_row_is_never_exposed_as_a_setting():
    vkey = settings._VERSION_KEY
    assert vkey not in settings.DEFAULTS, (
        "the internal version row must not be a user-facing setting"
    )
    assert vkey not in settings.all_values(), (
        "the version row leaked into all_values() -- it would render on the settings "
        "page and in `config list`"
    )
    with Session(engine) as s:
        row = s.exec(select(Setting).where(Setting.key == vkey)).first()
    assert row is not None and int(row.value) >= 1, ("the version row is missing after saves", row)
    print(
        "  [F] the version row lives in the Setting table but is invisible to every "
        "settings surface OK"
    )


def test_f2_the_version_advances_once_per_save_and_never_backwards():
    """ "Never backwards" holds for SAVES, which is what this pins. There is exactly one
    documented exception, and it is deliberate: the poison self-heal in _bump_version
    restarts the sequence at 1, so (..., 412, poke, 1) is possible. Harmless by
    construction -- every reader compares the version with `!=`, never `>`, so a
    backwards move is detected as a change exactly like a forwards one (test_f3 proves
    propagation survives it)."""

    def current():
        with Session(engine) as s:
            return int(
                s.exec(select(Setting).where(Setting.key == settings._VERSION_KEY)).first().value
            )

    start = current()
    for n in range(5):
        settings.set_many({"org_name": "v%d" % n})  # noqa: UP031
    after = current()
    assert after == start + 5, ("the version must advance exactly once per save", start, after)
    _write_from_another_process({"org_name": "from-elsewhere"})
    assert current() == after + 1, "a foreign process's save must advance it too"
    print("  [F2] the version advances exactly once per save, from either process OK")


def test_f3_the_version_row_cannot_be_set_or_poisoned():
    def current():
        with Session(engine) as s:
            r = s.exec(select(Setting).where(Setting.key == settings._VERSION_KEY)).first()
            return r.value if r else None

    settings.set_many({settings._VERSION_KEY: "nonsense", "org_name": "guarded"})
    assert settings.get("org_name") == "guarded", "the real key must still be saved"
    assert current().isdigit(), (
        "set_many let a caller overwrite the internal version row",
        current(),
    )
    assert (
        settings.get(settings._VERSION_KEY, None) is None
        or settings.get(settings._VERSION_KEY, None).isdigit()
    )

    # A hand-poked garbage value must SELF-HEAL rather than break every future save
    # (CAST('' AS INTEGER) is a silent 0 on SQLite but a hard error on Postgres).
    with Session(engine) as s:
        row = s.get(Setting, settings._VERSION_KEY)
        row.value = "not-a-number"
        s.add(row)
        s.commit()
    settings.set_many({"org_name": "after the poison"})
    assert current() == "1", ("the version row did not self-heal", current())
    assert settings.get("org_name") == "after the poison"
    _write_from_another_process({"org_name": "still propagating"})
    assert _wait_for(lambda: settings.get("org_name"), "still propagating") is not None, (
        "propagation broke after the version row was poisoned + healed"
    )

    # THE SAME CASE, BUT ACTUALLY DISCRIMINATING. On SQLite CAST('not-a-number' AS
    # INTEGER) is a silent 0, so 0+1=1 is ALSO what a build with NO self-heal produces:
    # the '1' above cannot tell the healed path from the unhealed one on this backend
    # (it can on Postgres, where the CAST is a hard error -- see tests/pg_smoke.py).
    # A NUMERIC-PREFIXED garbage value discriminates on SQLite too: CAST('4242abc' AS
    # INTEGER) is 4242, so a missing self-heal lands 4243 while the heal lands 1.
    # The WARNING is asserted alongside it, so the self-heal PATH is pinned and not
    # merely its arithmetic.
    with Session(engine) as s:
        row = s.get(Setting, settings._VERSION_KEY)
        row.value = "4242abc"
        s.add(row)
        s.commit()
    said = _CapturedWarnings()
    with said:
        settings.set_many({"org_name": "after the numeric poison"})
    assert current() == "1", (
        "the self-heal did not run: the version row held '4242abc' and the save "
        "advanced it by CASTing the garbage (4242+1) instead of resetting it first",
        current(),
    )
    assert any("4242abc" in m for m in said.messages), (
        "the version row self-healed SILENTLY -- resetting an operator's poked value "
        "must say so, or the next person debugging a version that jumped to 1 has "
        "nothing to go on",
        said.messages,
    )
    assert settings.get("org_name") == "after the numeric poison"
    _write_from_another_process({"org_name": "propagating after the numeric poison"})
    assert (
        _wait_for(lambda: settings.get("org_name"), "propagating after the numeric poison")
        is not None
    ), "propagation broke after a numeric-prefixed poison + heal"
    print(
        "  [F3] the version row cannot be set through set_many and self-heals "
        "(loudly, and from a value SQLite would happily CAST) without breaking "
        "propagation OK"
    )


def test_f4_the_check_interval_is_short_enough_to_be_an_emergency_off():
    assert settings.__dict__["CHECK_INTERVAL_SECONDS"] > 0
    # read the SHIPPED value out of the source, not the one this suite lowered
    import ast

    src = open(os.path.join(_ROOT, "app", "services", "settings.py"), encoding="utf-8").read()  # noqa: SIM115
    tree = ast.parse(src)
    shipped = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "CHECK_INTERVAL_SECONDS" for t in node.targets
        ):
            shipped = ast.literal_eval(node.value)
    assert shipped is not None, "CHECK_INTERVAL_SECONDS is no longer a module constant"
    assert 0 < shipped <= 5.0, ("an emergency-off switch must land within a few seconds", shipped)
    print(f"  [F4] the shipped check interval is {shipped:.1f}s -- 'within seconds' OK")


def test_f5_bump_version_is_an_escape_hatch_that_forces_a_reread():
    """F-R1: after an out-of-band write to the `setting` table -- the case
    invalidate_cache()'s and forget()'s own docstrings both contemplate -- an
    operator's obvious recovery move is to re-save the SAME value through
    set_many(). Post-F6 that no longer signals anyone (it is the documented
    identical-value skip: version N -> N). settings.bump_version() is the escape
    hatch: it moves the shared version with NO value write of its own, and this pins
    that it actually forces a running process to re-read a change it never made
    itself."""
    settings.set_many({"org_name": "before-the-out-of-band-poke"})
    assert settings.get("org_name") == "before-the-out-of-band-poke"

    # Simulate the out-of-band write itself: a direct DB edit that bypasses set_many
    # entirely (a hand-edited DB, a migration) and so never bumps the version.
    with Session(engine) as s:
        row = s.get(Setting, "org_name")
        row.value = "poked-out-of-band"
        s.add(row)
        s.commit()

    def current_version():
        with Session(engine) as s2:
            return int(
                s2.exec(select(Setting).where(Setting.key == settings._VERSION_KEY)).first().value
            )

    start_version = current_version()

    _bump_version_from_another_process()

    assert current_version() == start_version + 1, (
        "settings.bump_version() from another process did not advance the shared "
        "version by exactly 1",
        start_version,
        current_version(),
    )
    assert _wait_for(lambda: settings.get("org_name"), "poked-out-of-band") is not None, (
        "the version moved but this process never re-read the out-of-band value -- "
        "the escape hatch does not actually force anyone to reload"
    )
    print(
        "  [F5] settings.bump_version() advances the shared version and forces a "
        "running process to re-read an out-of-band write, with no value write of "
        "its own OK"
    )


# ============================================================================== #
# G — the `_generation` guard: an in-flight MISS can never re-seed a stale value
# ============================================================================== #
# THE RACE (settings.py, the `if gen == _generation` in get()). A cache miss reads the
# DB with `_cache_lock` RELEASED -- it has to, or every miss would serialise the hot
# path behind a DB round trip. So this interleaving is available:
#
#   T1  get("k") misses, reads "old" from the DB              <- the parked window
#   T2  another PROCESS saves k="new" and bumps the version
#   T3  this process probes, sees the new version, CLEARS the cache
#   T4  T1 wakes up and writes "old" back into the now-clean cache
#
# and it is not a transient: at T3 this process ALREADY adopted the DB's version, so
# every later probe sees "unchanged" and no future probe ever repairs the cache. The
# process serves "old" until it restarts -- audit K3 all over again, from a window of
# microseconds. The guard makes T4 a no-op by comparing the cache generation the miss
# read against with the current one.
#
# Reproduced DETERMINISTICALLY (not by timing luck) with a Session wrapper that parks
# one designated miss exactly between its DB read and its write-back.
class _ParkingSession:
    """A drop-in for sqlmodel.Session that lets ONE designated cache miss PARK between
    its DB read and its write-back into _cache.

    Arming is per-KEY and one-shot: the first `s.get(Setting, armed_key)` marks its
    session, and that session blocks in __exit__ (which is where settings.get() leaves
    the `with Session(...)` block, i.e. after the read and before the cache write)
    until `release` is set. The version probe reads a different key, so it sails
    through -- which is what lets the test land a probe inside the parked window."""

    armed_key = None
    parked = threading.Event()
    release = threading.Event()

    def __init__(self, *a, **kw):
        self._inner = _REAL_SESSION(*a, **kw)
        self._park = False

    def __getattr__(self, name):
        if name == "_inner":  # never recurse during __init__
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def get(self, *a, **kw):
        cls = type(self)
        if len(a) >= 2 and a[1] == cls.armed_key:
            cls.armed_key = None  # one-shot
            self._park = True
        return self._inner.get(*a, **kw)

    def __exit__(self, *exc):
        result = self._inner.__exit__(*exc)
        if self._park:
            type(self).parked.set()
            type(self).release.wait(timeout=60)
        return result


def test_g_a_parked_cache_miss_cannot_reseed_a_stale_value():
    settings.set_many({"org_name": "stale-in-flight"})
    settings.get("smtp_host")  # reset the probe clock on an UNRELATED key
    settings.forget(["org_name"])  # so the next get("org_name") MISSES

    _ParkingSession.armed_key = "org_name"
    _ParkingSession.parked.clear()
    _ParkingSession.release.clear()
    settings.Session = _ParkingSession
    served = []
    miss = threading.Thread(target=lambda: served.append(settings.get("org_name")), daemon=True)
    try:
        miss.start()
        assert _ParkingSession.parked.wait(timeout=30), (
            "the miss never reached its write-back point -- the wrapper did not arm"
        )
        settings.Session = _REAL_SESSION  # everything from here is a real one
        gen_at_park = settings._generation

        # The miss is now holding "stale-in-flight", read a moment ago. Land the whole
        # cross-process change inside its window: a foreign write, then OUR probe.
        _write_from_another_process({"org_name": "the-only-truth"})
        assert _wait_for(lambda: settings.get("org_name"), "the-only-truth") is not None, (
            "the foreign write never propagated, so this test never reached the race "
            "it exists to pin"
        )
        assert settings._generation != gen_at_park, (
            "no cache generation moved while the miss was parked -- the invalidating "
            "clear never happened, so this run would prove nothing"
        )
    finally:
        _ParkingSession.release.set()
        miss.join(timeout=30)
        settings.Session = _REAL_SESSION
        _ParkingSession.armed_key = None

    assert not miss.is_alive(), "the parked miss never finished"
    assert served == ["stale-in-flight"], (
        "the parked read should return the value it actually read -- it is a "
        "legitimately-timed read, and returning it to ITS caller is fine",
        served,
    )

    # THE ASSERTION. The stale write-back must not have landed, and -- because this
    # process's _version already matches the DB -- 'it will fix itself on the next
    # probe' is NOT available as a defence. If it landed, it is permanent.
    deadline = time.monotonic() + _TTL * 8
    while time.monotonic() < deadline:
        now = settings.get("org_name")
        assert now == "the-only-truth", (
            "a cache miss that started BEFORE a cross-process change re-seeded its "
            "stale reading AFTER the invalidating clear, and no probe will ever "
            f"repair it (this process's version already matches the DB). Serving {now!r}, "
            "the DB holds 'the-only-truth' -- audit K3, from a microsecond window"
        )
        time.sleep(_TTL / 4)
    print(
        "  [G] a cache miss parked across a foreign write + a probe cannot re-seed "
        "its stale reading (checked for %.2fs after the race) OK" % (_TTL * 8)
    )


def test_g2_the_same_guard_covers_a_local_save_and_a_forget():
    """The generation counter is bumped by set_many() and forget() too, not just by the
    probe -- an in-flight miss must lose to a LOCAL write for the same reason."""
    settings.set_many({"org_name": "local-stale"})
    settings.get("smtp_host")
    settings.forget(["org_name"])

    _ParkingSession.armed_key = "org_name"
    _ParkingSession.parked.clear()
    _ParkingSession.release.clear()
    settings.Session = _ParkingSession
    miss = threading.Thread(target=lambda: settings.get("org_name"), daemon=True)
    try:
        miss.start()
        assert _ParkingSession.parked.wait(timeout=30), "the wrapper did not arm"
        settings.Session = _REAL_SESSION
        settings.set_many({"org_name": "local-fresh"})  # lands INSIDE the window
    finally:
        _ParkingSession.release.set()
        miss.join(timeout=30)
        settings.Session = _REAL_SESSION
        _ParkingSession.armed_key = None
    assert settings.get("org_name") == "local-fresh", (
        "an in-flight miss overwrote a LOCAL set_many that committed while it was "
        "reading -- the save is durable in the DB but this process serves the old "
        "value: {!r}".format(settings.get("org_name"))
    )
    print("  [G2] an in-flight miss also loses to a local save that lands inside its window OK")


# ============================================================================== #
# H — one transaction: the version bump commits with the values, or not at all
# ============================================================================== #
class _WatchedConnection:
    """Marks its owning session the moment any statement referencing the version-row
    KEY runs through it -- not only the writes. _bump_version's leading sanity SELECT
    (its read-before-write guard against a poisoned row) passes the same {"k":
    _VERSION_KEY} parameter as its INSERT/UPDATE calls, so this flag actually fires on
    the first of _bump_version's statements, which happens to be that SELECT, not
    strictly "the moment it is written". Harmless for what this class is used for:
    _bump_version is the only code in this module that executes raw SQL on
    `s.connection()`, and all of its statements -- reads and writes alike -- belong to
    the one transaction under test, so "this transaction touched the version row" is
    still a precise 'this transaction carries the bump' flag."""

    def __init__(self, inner, owner):
        self._inner = inner
        self._owner = owner

    def __getattr__(self, name):
        if name in ("_inner", "_owner"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def execute(self, statement, parameters=None, *a, **kw):
        if isinstance(parameters, dict) and parameters.get("k") == settings._VERSION_KEY:
            self._owner.bumped = True
        if parameters is None:
            return self._inner.execute(statement, *a, **kw)
        return self._inner.execute(statement, parameters, *a, **kw)


class _ExplodingSession:
    """A Session whose commit() raises IF this transaction has already bumped the
    version row -- and only then.

    Under the shipped design that is THE transaction: the values were flushed into it
    and go down with it, so a save that cannot commit its version bump must leave the
    database exactly as it found it. If the bump were moved into its own session
    committed after the values (the mutation this is aimed at), the values would
    already be on disk when the version's commit blew up, and the install would sit at
    'new values, old version' -- unable to tell any other process that anything
    changed, forever."""

    def __init__(self, *a, **kw):
        self._inner = _REAL_SESSION(*a, **kw)
        self.bumped = False

    def __getattr__(self, name):
        if name in ("_inner", "bumped"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def connection(self, *a, **kw):
        return _WatchedConnection(self._inner.connection(*a, **kw), self)

    def commit(self):
        if self.bumped:
            raise RuntimeError("exploding commit: this transaction carries the bump")
        return self._inner.commit()


def _row_value(key):
    with _REAL_SESSION(engine) as s:
        row = s.get(Setting, key)
        return row.value if row is not None else None


def test_h_the_values_and_the_version_commit_or_fail_together():
    settings.set_many({"org_name": "before-the-explosion"})
    before_value = _row_value("org_name")
    before_version = _row_value(settings._VERSION_KEY)
    assert before_value == "before-the-explosion" and before_version is not None

    settings.Session = _ExplodingSession
    raised = None
    try:
        settings.set_many({"org_name": "must-not-land"})
    except Exception as exc:
        raised = exc
    finally:
        settings.Session = _REAL_SESSION

    assert raised is not None, (
        "the exploding commit did not surface -- a settings save that cannot commit "
        "must raise, never swallow"
    )
    assert _row_value("org_name") == before_value, (
        "the VALUES are on disk even though the transaction carrying the version bump "
        "failed. They are therefore published with a version that never moved, so no "
        "other process will EVER be told to re-read them: {!r}".format(_row_value("org_name"))
    )
    assert _row_value(settings._VERSION_KEY) == before_version, (
        "the version moved while the values did not -- a reader would drop its cache "
        "and re-read the OLD values, which is the harmless direction, but it still "
        "means the two are not one transaction"
    )

    # ...and the process is still coherent afterwards (the failed save left no
    # phantom in the cache).
    settings.invalidate_cache()
    assert settings.get("org_name") == before_value
    print(
        "  [H] a save whose version bump cannot commit moves NEITHER the values nor the version OK"
    )


class _TxnLog:
    """An ORDERED log of every statement (with the id of the connection it ran on) and
    every COMMIT/ROLLBACK on the app engine. This is what turns the 'bumped IN THE SAME
    TRANSACTION' comment in settings.py into an assertion."""

    def __init__(self):
        self.events = []

    def _stmt(self, conn, cursor, statement, params, ctx, many):
        self.events.append(["sql", id(conn), " ".join(statement.split()).lower(), repr(params)])

    def _commit(self, conn):
        self.events.append(["commit", id(conn), "", ""])

    def _rollback(self, conn):
        self.events.append(["rollback", id(conn), "", ""])

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._stmt)
        event.listen(engine, "commit", self._commit)
        event.listen(engine, "rollback", self._rollback)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._stmt)
        event.remove(engine, "commit", self._commit)
        event.remove(engine, "rollback", self._rollback)
        return False


def test_h2_one_save_is_one_transaction_on_one_connection():
    settings.set_many({"org_name": "h2-baseline"})
    with _TxnLog() as txn:
        settings.set_many({"org_name": "h2-changed"})
    events = txn.events

    def is_version(e):
        return e[0] == "sql" and (settings._VERSION_KEY in e[3] or "cast(" in e[2])

    def is_setting_write(e):
        return e[0] == "sql" and _WRITE_TO_SETTING.match(e[2]) is not None

    commits = [i for i, e in enumerate(events) if e[0] == "commit"]
    version_writes = [i for i, e in enumerate(events) if is_setting_write(e) and is_version(e)]
    value_writes = [i for i, e in enumerate(events) if is_setting_write(e) and not is_version(e)]

    assert len(commits) == 1, (
        "a settings save must be ONE transaction: %d COMMITs observed. Splitting the "  # noqa: UP031
        "version bump into its own transaction is exactly what lets a reader see new "
        "values with an old version (or an old version with new values)" % len(commits),
        [e[:3] for e in events],
    )
    assert value_writes, ("no value write was observed at all", [e[:3] for e in events])
    assert version_writes, ("the version row was never written by a save", [e[:3] for e in events])
    assert max(value_writes) < commits[0] and max(version_writes) < commits[0], (
        "the values and the version bump must BOTH be inside the one transaction, "
        "before its COMMIT",
        value_writes,
        version_writes,
        commits,
    )

    # NOTE ON THIS BACKEND: on SQLite the connection pool hands consecutive Sessions
    # the SAME underlying DBAPI connection, so this assertion is VACUOUS here -- it
    # PASSES even under a mutation that splits the bump into its own transaction on a
    # different Session object, because that second Session still surfaces as the
    # same pooled connection. It is `commits` (exactly one COMMIT) and the
    # `value_writes`/`version_writes` ordering above (both before that one commit)
    # that actually catch that mutation on THIS backend. This check is retained as a
    # CROSS-BACKEND guard: on Postgres, where a second Session can legitimately be
    # handed a different pooled connection, it is the one that would catch a bump
    # split onto its own connection.
    conns = {e[1] for e in events[: commits[0] + 1]}
    assert len(conns) == 1, (
        "the save spanned more than one connection before its commit -- the bump is "
        "not sharing the values' transaction",
        conns,
        [e[:3] for e in events],
    )
    assert not [e for e in events if e[0] == "rollback"], (
        "a clean save must not roll anything back",
        [e[:3] for e in events],
    )
    print(
        "  [H2] one save = 1 COMMIT on 1 connection, with both the value write and "
        "the version bump inside it OK"
    )


# ============================================================================== #
# I — a probe never leaves the cache fresh and the config module stale
# ============================================================================== #
def test_i_a_probe_with_no_prior_version_still_pushes_into_config():
    """invalidate_cache() forgets the observed VERSION as well as the values, so the
    next probe has nothing to compare against and takes the adoption path. In a
    long-running process that path MUST still run apply_to_config(): the provider /
    model / decrypted-cloud-key settings are PUSHED into the `config` module rather
    than read through get() at the call sites, so a refreshed value cache behind a
    stale config module is a half-applied change -- get() says one engine, every
    translation call uses another."""
    original = settings.DEFAULTS["default_provider"]
    try:
        settings.set_many({"default_provider": "mock"})
        assert config.DEFAULT_PROVIDER == "mock"

        _write_from_another_process({"default_provider": "local"})
        settings.invalidate_cache()
        assert settings._version is None, (
            "invalidate_cache must forget the version -- otherwise this test is "
            "exercising the ordinary change path, not the adoption path"
        )

        assert _wait_for(lambda: settings.get("default_provider"), "local") is not None, (
            "the value cache never refreshed"
        )
        assert config.DEFAULT_PROVIDER == "local", (
            "the VALUE cache refreshed to 'local' but config.DEFAULT_PROVIDER is still "
            f"{config.DEFAULT_PROVIDER!r}: the probe adopted the DB's version without pushing into the config "  # noqa: E501
            "module, and since the version now MATCHES, no later probe will ever "
            "notice. Every provider call keeps using the old engine"
        )

        # THE OTHER HALF, and the reason this is not simply "always apply": the very
        # FIRST version reading of a process must NOT re-apply. Boot's own
        # apply_to_config() is what is running at that moment, and pushing settings
        # into the config module EARLIER than a process expects changes what it sees
        # (a hermetic suite that blanks LOCAL_BASE_URL gets the shipped default
        # injected, which is how verify_qa_7_8_10_authoring caught the broad version
        # of this fix). Simulated by forgetting that this process ever saw a version.
        settings.set_many({"default_provider": "mock"})
        assert config.DEFAULT_PROVIDER == "mock"
        _write_from_another_process({"default_provider": "local"})
        settings.invalidate_cache()
        settings._version_seen_once = False  # as if the process just started
        assert _wait_for(lambda: settings.get("default_provider"), "local") is not None
        assert config.DEFAULT_PROVIDER == "mock", (
            "the FIRST version reading of a process re-applied into the config "
            "module. Boot is already applying at that point, and applying early "
            "moves values a booting process has not chosen yet",
            config.DEFAULT_PROVIDER,
        )
        assert settings._version_seen_once is True, (
            "the first adoption must record that this process now knows the version, "
            "or a later invalidate_cache would skip the push as well"
        )
    finally:
        settings._version_seen_once = True
        settings.set_many({"default_provider": original})
    assert original == config.DEFAULT_PROVIDER
    print(
        "  [I] a probe on the adoption path re-applies into the config module "
        "mid-life (post-invalidate_cache) but not at process start OK"
    )


# ============================================================================== #
# J — a no-op save costs every OTHER process nothing
# ============================================================================== #
def test_j_a_save_that_changes_nothing_does_not_invalidate_anyone():
    def version():
        return _row_value(settings._VERSION_KEY)

    settings.set_many({"org_name": "j-baseline"})
    at_rest = version()
    assert at_rest is not None and at_rest.isdigit()

    with _SettingReads() as spy:
        # Each of these three is a documented no-write path, and each is claimed
        # (settings.py, the comment above set_many's early return) to still run
        # apply_to_config() so a no-op save is byte-identical to a normal one
        # in-process. Merely checking config.DEFAULT_PROVIDER == settings.get(...)
        # afterward does NOT pin that claim: config was never wrong to begin with, so
        # a skipped apply_to_config() call is indistinguishable from one that ran (a
        # mutation deleting that call survives this suite otherwise). Tamper config
        # first so a skipped call is caught red-handed.
        config.DEFAULT_PROVIDER = "TAMPERED"
        settings.set_many({})  # nothing at all
        assert settings.get("default_provider") == config.DEFAULT_PROVIDER, (
            "set_many({}) (nothing to write at all) did not run apply_to_config() -- "
            f"config.DEFAULT_PROVIDER is still {config.DEFAULT_PROVIDER!r}"
        )

        config.DEFAULT_PROVIDER = "TAMPERED"
        settings.set_many({settings._VERSION_KEY: "999"})  # filters to empty
        assert settings.get("default_provider") == config.DEFAULT_PROVIDER, (
            "set_many() with only the internal version key (filters down to empty) "
            f"did not run apply_to_config() -- config.DEFAULT_PROVIDER is still {config.DEFAULT_PROVIDER!r}"  # noqa: E501
        )

        config.DEFAULT_PROVIDER = "TAMPERED"
        settings.set_many({"org_name": "j-baseline"})  # identical value
        assert settings.get("default_provider") == config.DEFAULT_PROVIDER, (
            "set_many() with only an identical-value key did not run "
            f"apply_to_config() -- config.DEFAULT_PROVIDER is still {config.DEFAULT_PROVIDER!r}"
        )

        # M12 sibling: a row skipped as "identical value" must still be SEEDED into
        # this process's cache via _cache.update(items) -- not merely left correct
        # because the DB already agrees. Reading it back right after must cost no
        # `setting` SELECT; if the skip stopped seeding the cache, this read would go
        # back to the DB (still return the right value, just not from the cache).
        reads_before = spy.selects
        assert settings.get("org_name") == "j-baseline"
        assert spy.selects == reads_before, (
            "a skipped (identical-value) row was not seeded into the cache -- "
            "get() had to hit the DB to answer it",
            spy.selects,
            reads_before,
        )
    assert version() == at_rest, (
        "a save that changes nothing bumped the cross-process version. Every OTHER "
        "process then drops its whole settings cache and re-runs apply_to_config to "
        "discover that nothing moved",
        at_rest,
        version(),
    )
    assert spy.writes == 0, (
        "a save that changes nothing still wrote to the setting table",
        spy.writes,
    )

    # ...with the in-process semantics of a save completely unchanged.
    assert settings.get("org_name") == "j-baseline"
    assert settings.get("default_provider") == config.DEFAULT_PROVIDER, (
        "a no-op save must still leave the config module applied"
    )

    # A REAL change still bumps exactly once...
    settings.set_many({"org_name": "j-changed"})
    assert int(version()) == int(at_rest) + 1, (
        "a real change must still bump exactly once",
        at_rest,
        version(),
    )
    assert settings.get("org_name") == "j-changed"

    # ...and so does a MIXED dict, where only some keys actually move.
    settings.set_many({"org_name": "j-changed", "smtp_from": "j@example.test"})
    assert int(version()) == int(at_rest) + 2, (
        "a dict with one unchanged and one changed key must bump exactly once",
        at_rest,
        version(),
    )
    assert settings.get("smtp_from") == "j@example.test"
    assert settings.get("org_name") == "j-changed"

    # A key with NO row yet is a real write even when its value equals the default.
    with Session(engine) as s:
        row = s.get(Setting, "smtp_user")
        if row is not None:
            s.delete(row)
            s.commit()
    settings.invalidate_cache()
    settings.set_many({"smtp_user": settings.DEFAULTS["smtp_user"]})
    assert _row_value("smtp_user") == settings.DEFAULTS["smtp_user"], (
        "an absent row must still be written, even when the value matches the default"
    )
    print(
        "  [J] empty / filtered-to-empty / identical saves write nothing and bump "
        "nothing; a real change still bumps exactly once OK"
    )


def main():
    test_a_an_out_of_process_write_reaches_a_running_process()
    test_a2_propagation_needs_no_restart_and_survives_repeats()
    test_b_the_kill_switch_stops_a_running_server()
    test_c_repeated_reads_within_the_interval_never_touch_the_db()
    test_c2_a_long_burst_costs_at_most_one_probe_per_interval()
    test_c3_a_cache_miss_still_costs_exactly_one_read()
    test_d_an_in_process_write_is_visible_immediately()
    test_d2_a_direct_db_poke_plus_invalidate_cache_still_works()
    test_d3_forget_still_evicts_without_writing()
    test_d4_defaults_and_semantics_are_untouched()
    test_e_concurrent_readers_and_a_writer_stay_consistent()
    test_e2_a_probe_never_holds_the_lock_across_the_db_read()
    test_f_the_version_row_is_never_exposed_as_a_setting()
    test_f2_the_version_advances_once_per_save_and_never_backwards()
    test_f3_the_version_row_cannot_be_set_or_poisoned()
    test_f4_the_check_interval_is_short_enough_to_be_an_emergency_off()
    test_f5_bump_version_is_an_escape_hatch_that_forces_a_reread()
    test_g_a_parked_cache_miss_cannot_reseed_a_stale_value()
    test_g2_the_same_guard_covers_a_local_save_and_a_forget()
    test_h_the_values_and_the_version_commit_or_fail_together()
    test_h2_one_save_is_one_transaction_on_one_connection()
    test_i_a_probe_with_no_prior_version_still_pushes_into_config()
    test_j_a_save_that_changes_nothing_does_not_invalidate_anyone()
    print("SETTINGS PROPAGATION OK")


if __name__ == "__main__":
    main()
