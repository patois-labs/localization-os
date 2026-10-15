"""Prompt Tap CROSS-PROCESS ROTATION SAFETY (GitHub #104, stress-0906).

THE DEFECT. `prompt_tap.capture()` appends to the shared legacy file and then rewrites
it in place (`_rotate_locked`) under `_LOCK` -- a *process-local* `threading.Lock`. A
headless operator (and this repo's own L2 lane) runs SEVERAL `advanced run` processes
against ONE `LOC_TMS_DATA_DIR`, so N processes raced on ONE fixed `captures.jsonl.tmp`
path:

  * Windows surfaced it LOUDLY as three different errors -- `[WinError 5]` and
    `[WinError 32]` on `os.replace(tmp, p)`, `[Errno 13]` on `open(tmp, "w")` -- each
    printed as a raw traceback into the operator's run output, each dropping a capture
    (twice the dropped record was the ledger's only record of a FAILED provider call);
  * POSIX would NOT have raised at all: two rotators interleave `writelines` into one
    tmp file and one of them publishes the mixture, or a rotation built from a stale
    read `os.replace`s away records another process appended in the meantime -- the
    same race, silent.

THE INVARIANT PROVEN HERE. Two SEPARATE PROCESSES hammer one ledger dir, hard enough
that rotation rewrites the file continuously:

  A  NOTHING IS LOST. Rotation may only ever evict the OLDEST records, so each writer's
     surviving sequence numbers must be a CONTIGUOUS SUFFIX of what it wrote -- no hole
     in the middle (a clobbered append) and no missing tail (a dropped capture).
  B  NOTHING IS CORRUPT. Every stored line is valid JSON written by one of the two
     writers, the file honours the record cap, and no `.tmp` debris is left behind.
  C  NOTHING IS SILENT AND NOTHING RAISES. Neither child prints the
     "prompt_tap: capture failed (ignored)" warning or a PermissionError traceback --
     on ANY OS. (capture() never raises by contract, so the traceback IS the symptom.)
  D  THE OPERATOR'S CLEAR BUTTON WINS (gate F round 1, M3b). `clear()` truncated under
     the process-LOCAL lock only, so a rotation committing its `os.replace` in another
     process put the pre-clear record set back over a file an operator had just cleared
     (POSIX) or raised `[WinError 5]` in that writer and dropped its capture (Windows).
     This is the one member of the #104 family with a PRIVACY consequence -- the button
     exists to drop captured prompt BODIES. Proven by holding the cross-process lock in
     one process while another clears: the clear must WAIT and the stale republish must
     land BEFORE it, leaving the file empty. Run TWICE -- healthy and with the L2 DEGRADE
     LATCH ARMED in the clearing process (gate F round 2, M1), because the latch added for
     the capture hot loop silently disarmed this serialization for 30 s after any capture's
     lock failure, invisibly to a suite whose processes always start unlatched.
  E  THE DEGRADE PATH ONLY APPENDS (gate F round 1, M3a). When the cross-process lock
     cannot be taken, `_append_and_rotate` performs ONE O_APPEND write and nothing else
     -- no rotation and, critically, no `_selfheal_file`, which is a read -> temp ->
     os.replace REWRITE of the whole file and therefore the very lost-update race this
     module closes. And a standing lock failure LATCHES (L2) instead of costing every
     capture a full 10 s poll loop on the translation thread.

Hermetic: throwaway shared LOC_TMS_DATA_DIR, no DB and no provider (the children patch
`enabled()` to True and lower MAX_RECORDS so rotation bites within a second). ASCII-only.
"""

import json
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_tapxproc_")

# Rotation cap the CHILDREN run with (the shipped 500 would need ~minutes of writes to
# bite). Small enough that every append past the first ~CAP rewrites the whole file.
CAP = 60
# Records per writer. 2 x 240 = 480 appends over a 60-record window: hundreds of
# rewrites, so a rotation race has many chances to land.
PER_WRITER = 240
WRITERS = ("A", "B")

_CHILD = r"""
import os, sys
sys.path.insert(0, %(root)r)
from app.services import prompt_tap

# No DB in the child: the tap's ON/OFF is a Setting read, and this test is about the
# FILE, not the toggle. Everything below capture() is exercised for real.
prompt_tap.enabled = lambda: True
prompt_tap.MAX_RECORDS = %(cap)d

tag, n = sys.argv[1], int(sys.argv[2])
for i in range(n):
    prompt_tap.capture(
        {
            "ts": "2026-09-06T00:00:00Z",
            "provider": "mock",
            "model": "m",
            "meta": {"writer": tag, "seq": i},
            "prompt": "p" * 160,
        }
    )
print("child %%s done" %% tag)
"""


def _run_writers(data_dir: str):
    """Spawn the two writer processes against `data_dir` and return their results."""
    child_py = os.path.join(data_dir, "_tap_writer.py")
    with open(child_py, "w", encoding="utf-8") as f:
        f.write(_CHILD % {"root": _ROOT, "cap": CAP})
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    procs = [
        subprocess.Popen(
            [sys.executable, child_py, tag, str(PER_WRITER)],
            cwd=_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for tag in WRITERS
    ]
    out = []
    for p in procs:
        so, se = p.communicate(timeout=600)
        out.append(
            (
                p.returncode,
                so.decode("utf-8", "replace"),
                se.decode("utf-8", "replace"),
            )
        )
    return out


def _read_records(path):
    """Every stored line, parsed. Raises on the first unparseable line (invariant B)."""
    recs = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError as exc:
                raise AssertionError(
                    f"captures.jsonl line {lineno} is not valid JSON ({exc}): {line[:120]!r}"
                ) from None
    return recs


def test_two_processes_lose_nothing():
    data_dir = tempfile.mkdtemp(prefix="shared_", dir=_TMP)
    os.makedirs(os.path.join(data_dir, "prompt_tap"), exist_ok=True)
    results = _run_writers(data_dir)

    # (C) both children exited clean, and NEITHER swallowed a capture failure. The
    # never-raise contract means a lost capture shows up ONLY as this warning.
    for tag, (rc, so, se) in zip(WRITERS, results, strict=True):
        assert rc == 0, f"writer {tag} exited {rc}\n{so}\n{se}"
        blob = so + se
        assert "capture failed" not in blob, (
            f"writer {tag} dropped at least one capture (the rotation race):\n{blob}"
        )
        assert "PermissionError" not in blob and "Traceback" not in blob, (
            f"writer {tag} hit a rotation error:\n{blob}"
        )

    path = os.path.join(data_dir, "prompt_tap", "captures.jsonl")
    assert os.path.exists(path), "no capture file was written"
    recs = _read_records(path)

    # (B) the cap is honoured and every line belongs to one of the two writers.
    assert len(recs) <= CAP, f"rotation left {len(recs)} records, cap is {CAP}"
    assert len(recs) == CAP, (
        f"expected the window to be full at {CAP} records, found {len(recs)} (records were lost)"
    )
    seqs = {tag: [] for tag in WRITERS}
    for r in recs:
        meta = r.get("meta") or {}
        w = meta.get("writer")
        assert w in seqs, f"unexpected record in the capture file: {r!r}"
        seqs[w].append(meta.get("seq"))

    # (A) rotation evicts the OLDEST records only, so what survives from each writer is
    # a CONTIGUOUS SUFFIX of that writer's appends. A hole = an append another process
    # clobbered; a short tail = a capture the race dropped outright.
    for tag in WRITERS:
        got = seqs[tag]
        assert got == sorted(got), f"writer {tag} records are out of order: {got!r}"
        if not got:
            continue  # legitimately evicted entirely by the other writer's newer records
        assert got == list(range(got[0], got[0] + len(got))), (
            f"writer {tag} lost records in the middle (hole in {got!r})"
        )
        assert got[-1] == PER_WRITER - 1, (
            f"writer {tag} lost its newest records (last surviving seq {got[-1]}, "
            f"wrote up to {PER_WRITER - 1})"
        )
    total = sum(len(v) for v in seqs.values())
    assert total == CAP, f"records unaccounted for: {total} of {CAP}"

    # (B) no rotation debris: a crashed/raced rewrite must not leave a tmp file behind.
    leftovers = [
        n
        for n in os.listdir(os.path.join(data_dir, "prompt_tap"))
        if n.endswith(".tmp") or ".tmp." in n
    ]
    assert not leftovers, f"rotation left temp debris behind: {leftovers!r}"
    print(
        f"  two writer processes x {PER_WRITER} records -> {len(recs)} kept, "
        "contiguous per writer, no drops OK"
    )


def test_single_process_rotation_unchanged():
    """The ordinary single-writer path is untouched by the cross-process guard: it still
    rotates to the cap, keeps the NEWEST records, and leaves no debris."""
    data_dir = tempfile.mkdtemp(prefix="solo_", dir=_TMP)
    os.makedirs(os.path.join(data_dir, "prompt_tap"), exist_ok=True)
    child_py = os.path.join(data_dir, "_tap_writer.py")
    with open(child_py, "w", encoding="utf-8") as f:
        f.write(_CHILD % {"root": _ROOT, "cap": CAP})
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, child_py, "S", str(PER_WRITER)],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        timeout=600,
    )
    blob = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    assert p.returncode == 0, blob
    assert "capture failed" not in blob, blob
    recs = _read_records(os.path.join(data_dir, "prompt_tap", "captures.jsonl"))
    assert len(recs) == CAP, f"solo writer kept {len(recs)} records, expected {CAP}"
    seqs = [(r.get("meta") or {}).get("seq") for r in recs]
    assert seqs == list(range(PER_WRITER - CAP, PER_WRITER)), (
        f"solo rotation must keep the NEWEST {CAP} records, got {seqs[0]!r}..{seqs[-1]!r}"
    )
    print(f"  single-writer rotation keeps the newest {CAP} records OK")


_HOLDER_SRC = r"""
import os, sys, time
sys.path.insert(0, %(root)r)
from app.services import prompt_tap

prompt_tap.enabled = lambda: True
p = prompt_tap._path()
# The stale read a rotation takes BEFORE it rewrites.
old = open(p, encoding="utf-8").read()
with prompt_tap._xproc_lock():
    open(os.path.join(os.path.dirname(str(p)), "holding.flag"), "w").close()
    time.sleep(%(hold)s)
    # ... and the os.replace it commits afterwards. Under the fix this happens while the
    # clearer is still WAITING for the lock, so the clear lands last and wins.
    tmp = str(p) + ".holder.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(old)
    os.replace(tmp, p)
print("holder done")
"""

_CLEARER_SRC = r"""
import sys, time
sys.path.insert(0, %(root)r)
from app.services import prompt_tap

prompt_tap.enabled = lambda: True
%(preamble)s
t0 = time.monotonic()
n = prompt_tap.clear()
print("cleared %%d in %%.2fs" %% (n, time.monotonic() - t0))
"""

# GATE F ROUND 2, M1. The clearer runs with the L2 DEGRADE LATCH ARMED. The latch was
# added so a CAPTURE on a translation thread stops paying a 10 s poll loop once the
# filesystem has proved it offers no advisory locking -- but `_xproc_lock` consulted it for
# EVERY caller, so for 30 s after ANY capture's lock failure `clear()` did not even attempt
# the lock and fell straight to the unserialized truncate M3b exists to prevent. Nothing
# could see it: a fresh test process always starts with the latch at 0.
_LATCH_PREAMBLE = (
    "prompt_tap._xproc_degraded_until = time.monotonic() + prompt_tap._XPROC_DEGRADE_S\n"
    'assert prompt_tap._xproc_degraded_until > time.monotonic(), "latch fixture broken"'
)

# How long the holder keeps the cross-process lock while the clearer waits for it. Well
# under _XPROC_LOCK_TIMEOUT_S (10 s) so the clear WAITS rather than degrading.
HOLD_S = 2.0


def _clear_race(latched: bool):
    """Run ONE clear-versus-rotating-writer race in two real processes and return
    (waited_seconds, records_left_on_disk). With `latched` the clearer arms the L2 degrade
    latch before calling `clear()`."""
    import time as _time

    data_dir = tempfile.mkdtemp(prefix="clear_", dir=_TMP)
    tap_dir = os.path.join(data_dir, "prompt_tap")
    os.makedirs(tap_dir, exist_ok=True)
    path = os.path.join(tap_dir, "captures.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for i in range(5):
            f.write(json.dumps({"ts": "2026-09-06T00:00:00Z", "meta": {"seq": i}}) + "\n")

    holder_py = os.path.join(data_dir, "_tap_holder.py")
    with open(holder_py, "w", encoding="utf-8") as f:
        f.write(_HOLDER_SRC % {"root": _ROOT, "hold": HOLD_S})
    clearer_py = os.path.join(data_dir, "_tap_clearer.py")
    with open(clearer_py, "w", encoding="utf-8") as f:
        f.write(_CLEARER_SRC % {"root": _ROOT, "preamble": _LATCH_PREAMBLE if latched else ""})
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"

    holder = subprocess.Popen(
        [sys.executable, holder_py],
        cwd=_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    flag = os.path.join(tap_dir, "holding.flag")
    deadline = _time.monotonic() + 30
    while not os.path.exists(flag) and _time.monotonic() < deadline:
        _time.sleep(0.02)
    assert os.path.exists(flag), "the holder never took the cross-process lock"

    t0 = _time.monotonic()
    clearer = subprocess.run(
        [sys.executable, clearer_py], cwd=_ROOT, env=env, capture_output=True, timeout=120
    )
    waited = _time.monotonic() - t0
    hso = clearer.stdout.decode("utf-8", "replace") + clearer.stderr.decode("utf-8", "replace")
    assert clearer.returncode == 0, hso
    hrc = holder.wait(timeout=120)
    hblob = holder.stdout.read().decode("utf-8", "replace") + holder.stderr.read().decode(
        "utf-8", "replace"
    )
    assert hrc == 0, hblob
    return waited, _read_records(path)


def test_clear_is_serialized_against_a_rotating_writer():
    """(D) An operator's Clear must not be undone by a rotation racing it in another
    process. The holder takes the cross-process lock, signals, sleeps, then commits an
    `os.replace` built from the read it took BEFORE the clear -- exactly what
    `_rotate_locked` does. The clearer runs during that window.

    Run TWICE: healthy, and with the L2 degrade latch ARMED in the clearing process (gate
    F round 2, M1). The latched arm is the one that failed before this round -- `clear()`
    consulted a latch that exists only to keep a 10 s poll loop off the TRANSLATION thread,
    so a capture's lock failure disarmed the operator's privacy button for 30 s."""
    for latched in (False, True):
        label = "with the L2 degrade latch ARMED" if latched else "healthy"
        waited, remaining = _clear_race(latched)
        assert not remaining, (
            f"({label}) a rotation racing the operator's Clear RESTORED the cleared "
            f"records ({len(remaining)} back on disk): the truncate did not hold the "
            "cross-process lock (M3b / gate F round 2 M1)"
        )
        assert waited >= HOLD_S * 0.5, (
            f"({label}) the clear returned in {waited:.2f}s while another process held the "
            f"lock for {HOLD_S}s -- it did not wait for it, so the serialization is not "
            "real. An operator's clear must IGNORE the capture path's degrade latch "
            "(gate F round 2, M1)."
        )
        print(f"  clear() {label}: waited {waited:.2f}s, 0 records restored OK")

    # And the same latch, in-process, must still gag the CAPTURE path it was added for --
    # otherwise "ignore the latch" was implemented by deleting the latch.
    _assert_latch_still_binds_the_capture_path()


def _assert_latch_still_binds_the_capture_path():
    """The M1 fix must EXEMPT clear()/the sweep, not DISARM the latch. Proven by counting
    `_xproc_acquire` attempts on both sides of one armed latch, in one process."""
    import time as _time

    from app.services import prompt_tap

    prompt_tap._dir().mkdir(parents=True, exist_ok=True)
    path = prompt_tap._path()
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"seq": 0}\n')

    real_acquire = prompt_tap._xproc_acquire
    attempts = {"n": 0}

    def counting_acquire(fd):
        attempts["n"] += 1
        return real_acquire(fd)

    prompt_tap._xproc_acquire = counting_acquire
    prompt_tap._xproc_degraded_until = _time.monotonic() + prompt_tap._XPROC_DEGRADE_S
    try:
        prompt_tap._append_and_rotate('{"seq": 1}')
        captured = attempts["n"]
        prompt_tap._xproc_degraded_until = _time.monotonic() + prompt_tap._XPROC_DEGRADE_S
        n = prompt_tap.clear()
    finally:
        prompt_tap._xproc_acquire = real_acquire
        prompt_tap._xproc_degraded_until = 0.0
    assert captured == 0, (
        "the capture path attempted the cross-process lock while the L2 latch was armed: "
        "the M1 fix disarmed the latch instead of exempting the operator paths"
    )
    assert attempts["n"] - captured == 1, (
        f"clear() made {attempts['n'] - captured} lock attempts under an armed latch, "
        "expected exactly 1 (gate F round 2, M1)"
    )
    assert n == 2, f"the clear fixture did not see both records: cleared {n}"
    with open(path, encoding="utf-8") as f:
        assert f.read() == "", "clear() did not empty the file"
    print(
        "  the latch still gags the CAPTURE path (0 attempts) and never gags clear() (1 attempt) OK"
    )


def test_degraded_path_appends_and_never_rewrites():
    """(E) With the cross-process lock unavailable, `_append_and_rotate` must do ONE
    O_APPEND write and nothing else. `_append_selfheal` is not safe here: on a
    PermissionError it runs `_selfheal_file`, a read -> temp -> os.replace rewrite of the
    whole file, which without the lock is the #104 lost-update race itself (silent on
    POSIX, an exception into capture()'s swallow on Windows). The writer-unique tmp name
    removed the tmp COLLISION, never the lost update."""
    import time as _time

    from app.services import prompt_tap

    prompt_tap._dir().mkdir(parents=True, exist_ok=True)
    path = prompt_tap._path()
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"seq": 0}\n')

    calls = {"selfheal": 0, "rotate": 0, "strict": 0}
    real = (
        prompt_tap._append_selfheal,
        prompt_tap._rotate_locked,
        prompt_tap._append_strict,
        prompt_tap._xproc_lock,
        prompt_tap._xproc_acquire,
    )

    def spy_selfheal(p, line):
        calls["selfheal"] += 1
        return real[0](p, line)

    def spy_rotate():
        calls["rotate"] += 1
        return real[1]()

    def spy_strict(p, line):
        calls["strict"] += 1
        return real[2](p, line)

    def dead_lock():
        raise prompt_tap._XProcLockUnavailable("forced unavailable")

    prompt_tap._append_selfheal = spy_selfheal
    prompt_tap._rotate_locked = spy_rotate
    prompt_tap._append_strict = spy_strict
    prompt_tap._xproc_lock = dead_lock
    try:
        prompt_tap._append_and_rotate('{"seq": 1}')
    finally:
        (
            prompt_tap._append_selfheal,
            prompt_tap._rotate_locked,
            prompt_tap._append_strict,
            prompt_tap._xproc_lock,
            prompt_tap._xproc_acquire,
        ) = real
    assert calls["strict"] == 1, f"the degraded path did not append: {calls!r}"
    assert calls["selfheal"] == 0, (
        "the degraded path ran the SELF-HEAL (a whole-file read -> temp -> os.replace "
        f"rewrite) with the cross-process lock NOT held -- that is the #104 race: {calls!r}"
    )
    assert calls["rotate"] == 0, f"the degraded path rewrote the file by rotating: {calls!r}"
    with open(path, encoding="utf-8") as f:
        body = f.read()
    assert '{"seq": 1}' in body and '{"seq": 0}' in body, (
        f"the degraded append lost a record: {body!r}"
    )

    # (E, L2) A STANDING lock failure latches: the second call must not pay the poll loop
    # again. Without the latch every capture costs _XPROC_LOCK_TIMEOUT_S (10 s) on the
    # translation thread, with every other capture thread queued behind _LOCK.
    acquires = {"n": 0}

    def slow_dead_acquire(fd):
        acquires["n"] += 1
        _time.sleep(0.4)  # stands in for the full poll loop
        raise prompt_tap._XProcLockUnavailable("no advisory locking on this filesystem")

    prompt_tap._xproc_degraded_until = 0.0
    prompt_tap._xproc_acquire = slow_dead_acquire
    try:
        t0 = _time.monotonic()
        prompt_tap._append_and_rotate('{"seq": 2}')
        first = _time.monotonic() - t0
        t1 = _time.monotonic()
        prompt_tap._append_and_rotate('{"seq": 3}')
        second = _time.monotonic() - t1
    finally:
        prompt_tap._xproc_acquire = real[4]
        prompt_tap._xproc_degraded_until = 0.0
    assert acquires["n"] == 1, (
        f"a persistent lock failure was retried on every capture ({acquires['n']} "
        "acquisitions): it must latch (L2)"
    )
    assert second < first / 2, (
        f"the second capture still paid the lock timeout ({second:.2f}s vs {first:.2f}s)"
    )
    with open(path, encoding="utf-8") as f:
        body = f.read()
    assert '{"seq": 2}' in body and '{"seq": 3}' in body, (
        f"the latched-degraded path dropped a record: {body!r}"
    )
    print(
        f"  degraded path: append only (no self-heal, no rotate); latch cut the second "
        f"capture from {first:.2f}s to {second:.2f}s OK"
    )


def main():
    test_two_processes_lose_nothing()
    test_single_process_rotation_unchanged()
    test_clear_is_serialized_against_a_rotating_writer()
    test_degraded_path_appends_and_never_rewrites()
    print("verify_tap_crossproc_rotation OK")


if __name__ == "__main__":
    main()
