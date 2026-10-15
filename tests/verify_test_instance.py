"""Guard suite for `scripts/test_instance.py` -- the tester-campaign instance tool.

This is the OPPOSITE-CONDITION half of the round-1 gate fixes
(the round-1 gate notes). Each check asserts that the failure the
gate found can no longer happen, by driving the real code path rather than re-reading it:

  H1  a FAILING `cli()` call whose argv carries `--admin-password` must produce a message with
      neither the password nor an un-blanked flag value -- including when the child echoes its
      own invocation back on stderr (the L3 attack shape).
  M1  `stop_server` must kill the process that actually OWNS the port, not just the pid it was
      handed, and must not claim success while something is still listening.
  M2  `up` must REFUSE a port that is already answering, and `wait_health` must fail fast when
      its own child died (rather than accept somebody else's 200).
  M3  the Advanced mission roster must name a real engine in EVERY committee slot, and
      `_mock_note` must say so ONLY when that is true.
  M4  a `memory-suggestions/generate` that proposed nothing must be a SKIP, not a seed.
  L1  the fail-open provider vars and both spellings of the install secrets must be blanked.
  L2  no `shell=True` anywhere in the module.
  L6  a junk pid file must read as 0, never raise.

It also guards the `--engines` option added 2026-09-08 (run the instance on the OLLAMA BOX
ONLY, leaving LM Studio untouched):

  E1  `--engines ollama` must point the install default (`local`) at the Ollama box and must not
      probe, configure, name or call LM Studio anywhere -- env, engine ring, workflow steps,
      Advanced roster, or instance.md;
  E2  `--engines both` must be byte-unchanged from before (anti-vacuity for every E1 check);
  E3  an unknown engine set is a refusal, not a silent default, and the refusal names every set.

and the `--engines lmstudio` set added 2026-09-16 (the Ollama box was down at campaign time twice
while LM Studio was up; a campaign lane that needs a REAL model must not depend on that box):

  E4  `--engines lmstudio` must run the instance on LM Studio ALONE: the Ollama box is never
      probed, never given an engine profile, never referenced by a roster slot, never called for
      the engine-#2 MT proof (which is recorded as SKIPPED with an honest reason, never as `ok`)
      and never presented in `instance.md` as present -- the facts sheet declares engine #2
      ABSENT. The child environment differs from a LIVE `both` environment in exactly two
      informational keys (`LOC_TMS_TEST_ENGINE_SET` and the now-blank
      `LOC_TMS_TEST_OLLAMA_BASE`), and the skipped engine-#2 row reports the seeded run's
      RECORDED outcome rather than claiming one.

and the round-2 gate fixes (the round-2 gate notes), labelled `R2-`:

  R2-M1  `down` must kill ONLY what it can prove is this instance. A live process whose command
         line carries no marker of this instance -- a recycled pid, a stranger on the recorded
         port -- must SURVIVE `down`, be reported, and make `down` exit non-zero.
  R2-L1  the MCP child's stderr must be scrubbed of the admin token before it can reach
         `instance.md` (`mcp_tool_count`) or the console (`mcp_call_tool`).
  R2-L2  `down`'s `mcp remove` echo must be scrubbed with this instance's own key values.
  R2-L3  a netstat that resolves no port owner must WARN and be recorded, never silently alias
         the listener to the supervisor.
  R2-L4  a `down` that leaves a listener behind must exit non-zero.
  R2-L5  `Seeder.step` must really scrub the reason it stores (its comment claimed a scrub the
         code did not do).
  R2-L6  the `cmd_up` refusal tests must be UNABLE to build an instance if the guard regresses.
  R2-L8  `start_server` must re-check the port immediately before the bind and abort cleanly.

and the round-3 gate fixes (the round-3 gate notes), labelled `R3-`:

  R3-M1  the marker that fires IN PRODUCTION must be instance-unique. A SECOND marked instance on
         the recorded port must SURVIVE a `down` of a retired root, a clean stop must retire the
         root (`server.port` gone, no instance.md port fallback), and the fake server must carry
         its marker the way the real uvicorn does -- INSIDE the `-c` snippet, not as its own argv
         token (the old shape tested a rung production never used).
  R3-M2  `up` must not deadlock against `down`: a recorded pid that is provably NOT ours is a
         stale file, not "a live server", in both verbs.
  R3-L1  identity is re-asserted (`CreationDate`) immediately before the kill.
  R3-L2  a refused `down` must NOT remove the MCP registration.
  R3-L3  a refusal must not echo a foreign process's command line.
  R3-L4  a CIM query that could not RUN must be diagnosed by name, not reported as a verdict.
  R3-L5  the skip-reason scrub reads `api._token` directly, so a rename breaks loudly.
  R3-L6  the suite patches `test_instance`'s own indirections (`ti._run`, `ti._popen`, `ti._sleep`,
         `ti._rmtree`), never the shared stdlib module attributes, and proves it at the end.

and the round-4 gate fixes (the round-4 gate notes), labelled `R4-`:

  R4-H1  a LIVE python running `app.main:app` with no stamp is a LEGACY instance -- ours but
         unprovable -- not a stranger: its pid files are KEPT, nothing is killed, "`up` can run
         again" is never printed while a listener is alive, `up` refuses over it, and
         `down --legacy-pid` is the sanctioned teardown (accepted only while /health answers on
         the recorded port and that port's owner is an UNSTAMPED `app.main:app` python).
  R4-M1  the stamp is TERMINATED (`# loc-test-instance:<root>#`), so a name-prefix root cannot
         claim another instance's processes, and a root with no pid file has no port to act on
         at all -- which disarms the roots retired by earlier builds.
  R4-L1  the port arm's creation-date re-check ignores an EMPTY second read, like the pid arm.
  R4-L3  the CIM-failure warning is latched once per command.

and the round-5 gate fixes (the round-5 gate notes), labelled `R5-`:

  R5-H1  a command line carrying ANOTHER root's stamp is a STRANGER, never the "ours but
         unprovable" LEGACY verdict, and `--legacy-pid` licenses a PID rather than a call: only a
         process that is BOTH a current owner of this root's recorded port AND an unstamped
         `app.main:app` python. The arm round 4 never covered -- the port owner and the recorded
         pid being DIFFERENT processes -- is the one driven here.
  R5-M1  the `--legacy-pid` precondition is evaluated LAZILY, so a stray flag on a healthy modern
         instance is byte-for-byte a plain `down` (exit 0, files gone, MCP unregistered).
  R5-M2  `cmd_up`'s recorded-port guard reads `server.port` ungated, so the escape hatch's own
         "delete the pid files by hand" cannot re-open the `_rmtree` over a live server.
  R5-L1  the stamped-pid refusal distinguishes THIS root's own stamp from another root's.
  R5-L2  `status` falls back to `server.port` (labelled) for a live-but-hand-cleared root.

and the round-6 gate fix (the round-6 gate notes), labelled `R6-`:

  R6-H1  `--legacy-pid` reaches BOTH halves of the canonical legacy instance: uvicorn `workers=1`
         forks the worker that owns the port, so the licence covers an unstamped `app.main:app`
         owner of this root's recorded port AND the parent supervisor of one. Requiring ownership
         alone left the supervisor alive and uvicorn respawned the worker. A sibling unstamped
         python that owns no port and parents no owner is still refused, in the same call.

Contract with tests/test_verify_suites.py: run as its own subprocess from the repo root; exit 0
and print a final line ending in `OK`. The process-level checks (M1, M2's live-port arm) use
Windows process tooling, which is all `scripts/test_instance.py` itself supports; on any other
platform they are announced as skipped and the rest still runs.
"""

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

os.environ["LOC_TMS_TEST_OLLAMA_HOSTS"] = "http://192.0.2.10:11434,http://192.0.2.11:11434"

import test_instance as ti

IS_WINDOWS = sys.platform == "win32"
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {label}")
    else:
        FAILURES.append(f"{label}: {detail}")
        print(f"  FAIL {label}: {detail}")


# ---------------------------------------------------------------------------------------
# H1 -- a failing CLI call must not print the password
# ---------------------------------------------------------------------------------------
_PW = "Tt1SuperSecretPasswordValue"


class _FakeProc:
    """A child that FAILED and, like many CLIs, quoted its own invocation back on stderr."""

    def __init__(self, argv):
        self.returncode = 1
        self.stdout = ""
        self.stderr = "usage error near: " + " ".join(str(a) for a in argv)


def h1_failing_cli_message_has_no_password() -> None:
    # R3-L6: `ti._run`, the module's own indirection -- NOT `subprocess.run`, which is a shared
    # stdlib module attribute every other test in this interpreter also uses.
    real_run = ti._run
    ti._run = lambda argv, **kw: _FakeProc(argv)  # type: ignore[assignment]
    try:
        try:
            ti.cli(
                {},
                "init",
                "--admin-email",
                "admin@x.test",
                "--admin-password",
                _PW,
                "--admin-name",
                "Test Admin",
            )
            check("H1 failing cli() raises", False, "no Abort was raised")
            return
        except ti.Abort as exc:
            message = str(exc)
    finally:
        ti._run = real_run
    check("H1 failing cli() message omits the password", _PW not in message, message[:200])
    check("H1 failing cli() message keeps the flag (redacted)", "--admin-password ***" in message)
    check("H1 the non-secret argv survives", "--admin-email admin@x.test" in message)

    # the same shape for `user create --password`, and for `--flag=value`
    redacted = ti.redact_argv(["user", "create", "--password", _PW, "--role", "viewer"])
    check("H1 redact_argv blanks --password", _PW not in redacted, redacted)
    eq_form = ti.redact_argv(["engine", "create", f"--secret=api_key={_PW}"])
    check("H1 redact_argv blanks --flag=value", _PW not in eq_form, eq_form)
    check("H1 scrub blanks a value anywhere", _PW not in ti.scrub(f"boom {_PW} boom", _PW))
    # ANTI-VACUITY: the redactor must not simply blank everything.
    plain = ti.redact_argv(["config", "features", "--json"])
    check("H1 redactor leaves non-secret argv alone", plain == "config features --json", plain)


# ---------------------------------------------------------------------------------------
# M1 / M2 -- port ownership, refusal, and a `down` that really frees the port
# ---------------------------------------------------------------------------------------
class _FakePopen:
    """Just the `.pid`/`.poll()` surface `record_pids` reads off the launched child."""

    def __init__(self, pid: int, alive: bool = True):
        self.pid = pid
        self.returncode = None if alive else 9

    def poll(self):
        return self.returncode


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# A stand-in for the instance's uvicorn: it answers /health 200 (so `stop_server`'s orphan arm is
# reachable) and it carries a MARKER on its command line. That marker is the whole point of the
# round-2 M1 fix: `down` decides what to kill from `Win32_Process.CommandLine`, so the test server
# must be able to look either like this instance (marker = this root's stamp) or like a stranger
# (marker = another root's stamp, or something that is not a stamp at all) with nothing else about
# it changing.
#
# ROUND-3 M1: the marker rides INSIDE the `-c` snippet, as a trailing comment, exactly the shape
# `start_server` produces (`python.exe -c "import uvicorn; uvicorn.run(...)  # loc-test-instance:
# <root>"`). The old fake passed it as its own argv token -- a shape the real launch NEVER has --
# so the positive test proved a rung production could not use, and the gate's third scenario (a
# second real instance on the recorded port) went untested.
_FAKE_SERVER = """import http.server, sys


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, *a):
        pass


http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def _start_fake_server(port: int, marker: str) -> subprocess.Popen:
    """Launch the fake listener with `marker` (a `#` comment line) inside the `-c` snippet."""
    proc = subprocess.Popen(
        [sys.executable, "-c", f"{_FAKE_SERVER}\n{marker}\n", str(port)],
        creationflags=ti.DETACHED,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        if ti.port_owner_pids(port):
            break
        time.sleep(0.25)
    return proc


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60)


def m1_down_kills_the_real_listener() -> None:
    if not IS_WINDOWS:
        print("  skip M1/M2-live (Windows-only process tooling)")
        return
    port = _free_port()
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    # The marker `pid_is_ours` looks for, in the shape production produces: this root's stamp,
    # inside the `-c` snippet.
    proc = _start_fake_server(port, ti._stamp_for(root))
    try:
        owners = ti.port_owner_pids(port)
        check("M1 port_owner_pids finds a listener", bool(owners), f"{owners} vs {proc.pid}")
        check("M2 port_is_free is False while it listens", not ti.port_is_free(port))

        # record_pids must write the pid that OWNS the port, not the pid we launched.
        listener, supervisor, note = ti.record_pids(root, port, _FakePopen(proc.pid))
        check(
            "M1 record_pids returns the port owner", listener in owners, f"{listener} vs {owners}"
        )
        check("M1 record_pids keeps the supervisor separately", supervisor == proc.pid)
        check("R2-L3 a resolved owner produces no fallback note", note == "", note)
        check(
            "M1 server.pid holds the LISTENER, not the supervisor",
            ti._read_pid(root / "server.pid") == listener
            and ti._read_pid(root / "server.supervisor.pid") == proc.pid,
        )
        # OPPOSITE CONDITION: a port owned by a stranger is refused, never adopted.
        try:
            ti.record_pids(root, port, _FakePopen(999997))
            check("M2 record_pids refuses a foreign listener", False, "no Abort")
        except ti.Abort as exc:
            check(
                "M2 record_pids refuses a foreign listener", "ANOTHER" in str(exc), str(exc)[:160]
            )

        # R3-M1: the launched argv really carries the stamp the matcher reads -- i.e. the marker
        # is on the COMMAND LINE the OS reports, not only in a string this suite made up.
        cmdline, failure = ti._command_line(listener)
        check(
            "R3-M1 the marker is inside the -c snippet on the real command line",
            ti._stamp_for(root) in cmdline and " -c " in cmdline,
            f"{failure} :: {len(cmdline)} chars",
        )
        check(
            "R3-M1 the bare uvicorn snippet is NOT a marker any more",
            ti._UVICORN_SNIPPET.format(port=port) not in ti.instance_markers(root, port),
            str(ti.instance_markers(root, port))[:200],
        )

        # R2-M1: the identity check itself, both ways, against one real live process.
        ours, why = ti.pid_is_ours(listener, root, port)
        check("R2-M1 pid_is_ours accepts a marked command line", ours, why)
        stranger, why_not = ti.pid_is_ours(os.getpid(), root, port)
        check("R2-M1 pid_is_ours rejects an unrelated live pid", not stranger, why_not)
        # R3-L3: the refusal must not echo the foreign process's command line back at the console.
        check(
            "R3-L3 a refusal names the pid, not the foreign command line",
            "foreign process" in why_not and sys.executable not in why_not,
            why_not,
        )
        check(
            "R2-M1 pid_is_ours accepts a child of an already-proven pid",
            ti.pid_is_ours(listener, root, 0, trusted_parents=[ti._parent_pid(listener)])[0]
            if ti._parent_pid(listener)
            else True,
        )

        # THE ORPHAN SHAPE: the pid files name DEAD pids (supervisor killed alone, worker still
        # serving). `down` must still free the port -- the owner proves itself ours by command
        # line -- and must not report success while something is listening.
        (root / "server.port").write_text(str(port), encoding="utf-8")
        (root / "server.pid").write_text("999999", encoding="utf-8")
        (root / "server.supervisor.pid").write_text("999998", encoding="utf-8")
        note, ok = ti.stop_server(root)
        check(
            "M1 stop_server frees the port despite a stale pid file",
            not ti.port_owner_pids(port),
            note,
        )
        check(
            "M1 stop_server names the port owner it killed", f"port-{port} owner pid" in note, note
        )
        check("M1 stop_server does not claim a listener remains", "WARNING" not in note, note)
        check("R2-L4 a clean stop reports ok", ok, note)
        left = [
            n for n in ("server.pid", "server.supervisor.pid", "server.port") if (root / n).exists()
        ]
        check("R3-M1 a finished stop RETIRES the root (pid files AND server.port)", not left, left)
        check("R3-M1 a retired root reports no port at all", ti._read_port(root) == 0)
    finally:
        if proc.poll() is None:
            _kill_tree(proc.pid)
        for name in ("server.pid", "server.supervisor.pid", "server.port"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def r2m1_down_refuses_a_listener_that_is_not_ours() -> None:
    """THE NEGATIVE: a foreign process on the recorded port must SURVIVE `down`.

    Round-2 gate M1: both `down` arms killed on a NUMBER. A recorded pid outlives a reboot and
    Windows recycles pids, and the recorded PORT can be taken by a second test instance, a dev
    server or a tunnel -- all of which the old `stop_server` killed (with `/T`, so their whole
    trees) while reporting `killed port-N owner pid P` as success. Here the same process is both:
    the recorded `server.pid` AND the port owner, alive, serving /health 200, and carrying NO
    marker of this instance. It must still be running afterwards."""
    if not IS_WINDOWS:
        print("  skip R2-M1-live (Windows-only process tooling)")
        return
    port = _free_port()
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    proc = _start_fake_server(port, "# somebody-elses-server")
    try:
        owners = ti.port_owner_pids(port)
        check("R2-M1 the foreign listener is up", bool(owners), str(owners))
        (root / "server.port").write_text(str(port), encoding="utf-8")
        (root / "server.pid").write_text(str(owners[0]), encoding="utf-8")
        (root / "server.supervisor.pid").write_text("999998", encoding="utf-8")

        note, ok = ti.stop_server(root)
        check("R2-M1 down REFUSES a foreign listener", not ok, note)
        check("R2-M1 the refusal says whose port it is", "not this instance" in note, note)
        check("R2-M1 the refusal says it did not kill", "not killing it" in note, note)
        still = ti.port_owner_pids(port)
        check("R2-M1 the foreign process SURVIVED down", still == owners, f"{still} vs {owners}")
        check("R2-M1 the foreign pid is still alive", ti._pid_alive(owners[0]))
        # R3-M2: the pid file that named the stranger is worthless -- it is deleted, so `up` and a
        # repeat `down` are not deadlocked by it -- while the refusal itself still stands.
        check(
            "R3-M2 the pid file naming the foreign process was deleted as stale",
            not (root / "server.pid").exists(),
            note,
        )
        check("R3-M2 the note says which file it dropped", "THAT FILE WAS DELETED" in note, note)
        check(
            "R3-M2 the refusal names the escape hatch (which files, where)",
            "ESCAPE HATCH" in note and str(root) in note,
            note,
        )
        # A REPEAT `down` must not repeat the identical refusal forever: the stale file is gone,
        # so the second call has nothing to refuse about the pid (the port arm still protects the
        # stranger).
        note2, ok2 = ti.stop_server(root)
        check(
            "R3-M2 a repeat down no longer refuses on the recorded pid",
            "belongs to another process" not in note2,
            note2,
        )
        check("R3-M2 the stranger is still protected by the port arm", not ok2, note2)
        check("R3-M2 and is still alive after the repeat", ti._pid_alive(owners[0]), note2)
        # ANTI-VACUITY: the same call shape with OUR marker on the command line does kill --
        # proved in m1_down_kills_the_real_listener above, which shares this code path.
    finally:
        if proc.poll() is None:
            _kill_tree(proc.pid)
        for name in ("server.pid", "server.supervisor.pid", "server.port"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def r3m1_a_second_instance_on_the_same_port_survives_a_retired_down() -> None:
    """ROUND-3 M1, the scenario the marker was not unique enough to stop.

    Root A was `down`ed and retired; instance B later came up on the same port (ports in this
    workflow are fixed by the RUNBOOK and REUSED). A stray `down A` -- a cleanup script walking
    `.test-instances/*`, or a tester re-running a command -- used to take the port arm, match B on
    the shared `app.main:app`+port snippet, `taskkill /T` B's whole tree and exit 0.

    B here carries B's OWN stamp in the same argv shape production uses, and A's root is armed the
    way the ORPHAN case arms it: a `server.port` naming the shared port and a recorded pid that is
    dead (round-4 M1 disarms a root with no pid file at all, so a `server.port` alone no longer
    reaches the port arm -- that disarm has its own check below)."""
    if not IS_WINDOWS:
        print("  skip R3-M1-live (Windows-only process tooling)")
        return
    port = _free_port()
    root_a = Path(tempfile.mkdtemp(prefix="loctms_ti_a_"))
    root_b = Path(tempfile.mkdtemp(prefix="loctms_ti_b_"))
    proc = _start_fake_server(port, ti._stamp_for(root_b))
    try:
        owners = ti.port_owner_pids(port)
        check("R3-M1 instance B is listening", bool(owners), str(owners))
        (root_a / "server.port").write_text(str(port), encoding="utf-8")
        (root_a / "server.pid").write_text("999999", encoding="utf-8")

        note, ok = ti.stop_server(root_a)
        check("R3-M1 down on the RETIRED root refuses", not ok, note)
        check("R3-M1 the refusal says the port owner is not this instance", "not this" in note)
        still = ti.port_owner_pids(port)
        check("R3-M1 the second instance SURVIVED", still == owners, f"{still} vs {owners}")
        check("R3-M1 the second instance is still alive", ti._pid_alive(owners[0]))

        # ANTI-VACUITY: the identical call from B's OWN root kills B. Same process, same port,
        # same code path -- only the root (and therefore the stamp) differs.
        (root_b / "server.port").write_text(str(port), encoding="utf-8")
        (root_b / "server.pid").write_text("999999", encoding="utf-8")
        note2, ok2 = ti.stop_server(root_b)
        check("R3-M1 B's own down does kill B", ok2, note2)
        check("R3-M1 the port is free afterwards", not ti.port_owner_pids(port), note2)
    finally:
        if proc.poll() is None:
            _kill_tree(proc.pid)
        for root in (root_a, root_b):
            for name in ("server.pid", "server.supervisor.pid", "server.port"):
                (root / name).unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                root.rmdir()


def r3m1_a_retired_root_has_no_port_to_act_on() -> None:
    """R3-M1: `_read_port`'s instance.md fallback used to re-arm a root that had been `down`ed."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    try:
        (root / "instance.md").write_text(
            "- Name: `x`  ·  port: `8361`  ·  base URL: `http://127.0.0.1:8361`\n",
            encoding="utf-8",
        )
        check("R3-M1 a retired root (no pid file) reports no port", ti._read_port(root) == 0)
        # ANTI-VACUITY: while a pid file is on disk the root still claims a server, and the
        # instance.md fallback still resolves the port for instances built before `server.port`.
        (root / "server.pid").write_text("4242\n", encoding="utf-8")
        check(
            "R3-M1 the instance.md fallback still works while a pid file exists",
            ti._read_port(root) == 8361,
            str(ti._read_port(root)),
        )
        (root / "server.port").write_text("8399\n", encoding="utf-8")
        check("R3-M1 server.port still wins over instance.md", ti._read_port(root) == 8399)
    finally:
        for name in ("server.pid", "server.port", "instance.md"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def r3l1_identity_is_reasserted_before_the_kill() -> None:
    """R3-L1: the ownership verdict costs a CIM round trip, so it is ~a second old at the kill.

    A proven pid that exits in that window and whose NUMBER is immediately reused must not be
    killed. Driven through the module's own indirections: `_creation_date` returns a DIFFERENT
    creation time on the second read, i.e. the pid turned over."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    (root / "server.pid").write_text("4242", encoding="utf-8")
    real = (ti._pid_alive, ti.pid_is_ours, ti._kill, ti._creation_date, ti.port_owner_pids)
    killed: list[int] = []
    reads = iter(["20260908010101.000000-000", "20260908999999.000000-000"])
    ti._pid_alive = lambda pid: True
    ti.pid_is_ours = lambda pid, r, p, trusted_parents=(): (True, "stub: ours")
    ti._kill = lambda pid: killed.append(pid)
    ti._creation_date = lambda pid: next(reads, "20260908999999.000000-000")
    ti.port_owner_pids = lambda p: []
    try:
        note, ok = ti.stop_server(root)
        check("R3-L1 a pid that turned over is not killed", not killed, str(killed))
        check("R3-L1 the recycle is reported", "REPLACED between" in note, note)
        check("R3-L1 and it is not success", not ok, note)
        # ANTI-VACUITY: a creation time that does NOT change is the same process, and it dies.
        (root / "server.pid").write_text("4242", encoding="utf-8")
        ti._creation_date = lambda pid: "20260908010101.000000-000"
        note2, ok2 = ti.stop_server(root)
        check("R3-L1 a stable creation time still kills", killed == [4242], str(killed))
        check("R3-L1 and that is success", ok2, note2)
    finally:
        (ti._pid_alive, ti.pid_is_ours, ti._kill, ti._creation_date, ti.port_owner_pids) = real
        for name in ("server.pid", "server.supervisor.pid", "server.port"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def r3l2_a_refused_down_keeps_the_mcp_registration() -> None:
    """R3-L2: the unregister used to run BEFORE the `ok` test, so a `down` that killed nothing
    still deleted the MCP entry documenting a server that is still serving a live admin key."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    real_root_for, real_stop, real_unreg = ti._root_for, ti.stop_server, ti.mcp_unregister
    removed: list[str] = []
    ti._root_for = lambda name: root
    ti.mcp_unregister = lambda name: removed.append(name) or "removed"
    try:
        ti.stop_server = lambda r, legacy_pid=False: ("WARNING: ... -- not killing it", False)
        rc = ti.cmd_down("verifyprobe")
        check("R3-L2 a refused down does not unregister MCP", not removed, str(removed))
        check("R3-L2 a refused down still exits non-zero", rc == 2)
        # ANTI-VACUITY: a clean stop DOES remove the registration.
        ti.stop_server = lambda r, legacy_pid=False: ("killed listener pid 1", True)
        rc2 = ti.cmd_down("verifyprobe")
        check("R3-L2 a clean down still unregisters MCP", removed == ["verifyprobe"], str(removed))
        check("R3-L2 a clean down exits 0", rc2 == 0)
    finally:
        ti._root_for, ti.stop_server, ti.mcp_unregister = real_root_for, real_stop, real_unreg
        with contextlib.suppress(OSError):
            root.rmdir()


def r3m2_up_is_not_deadlocked_by_a_recycled_pid() -> None:
    """R3-M2: `cmd_up` decided "there is a live server here" from a bare `_pid_alive`.

    After a reboot Windows may have handed `server.pid`'s number to an unrelated process. `down`
    then refused to kill it (correctly) and `up` refused to run (pointing at `down`), so the
    instance could be neither started nor stopped. `up` must now ask the same question `down`
    asks and treat a provably-foreign pid as a stale file. Driven behind the R2-L6 tripwires, so
    a regressed guard cannot build an instance from inside the suite."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    real_root_for = ti._root_for
    ti._root_for = lambda name: root
    try:
        # This interpreter is a live pid that is emphatically NOT this instance.
        (root / "server.pid").write_text(str(os.getpid()), encoding="utf-8")
        with _no_side_effects():
            try:
                ti.cmd_up("verifyprobe", _free_port())
                check("R3-M2 up runs past a recycled pid file", False, "no _Tripwire")
            except _Tripwire:
                check("R3-M2 up runs past a recycled pid file", True)
            except ti.Abort as exc:
                check("R3-M2 up runs past a recycled pid file", False, f"aborted: {exc}"[:200])
        check("R3-M2 the stale pid file was deleted", not (root / "server.pid").exists())

        # ANTI-VACUITY: the SAME live pid, with ownership proven, is still a hard refusal --
        # `up` must never rebuild a root out from under a running instance.
        (root / "server.pid").write_text(str(os.getpid()), encoding="utf-8")
        real_ours = ti.pid_is_ours
        ti.pid_is_ours = lambda pid, r, p, trusted_parents=(): (True, "stub: ours")
        try:
            with _no_side_effects():
                try:
                    ti.cmd_up("verifyprobe", _free_port())
                    check("R3-M2 up still refuses a live server that IS ours", False, "no Abort")
                except ti.Abort as exc:
                    message = str(exc)
                    check(
                        "R3-M2 up still refuses a live server that IS ours",
                        "already has a live server" in message,
                        message[:200],
                    )
                    check(
                        "R3-M2 the refusal names the escape hatch (which files, where)",
                        "server.supervisor.pid" in message and str(root) in message,
                        message[:300],
                    )
                except _Tripwire as exc:
                    check("R3-M2 up still refuses a live server", False, f"GUARD REGRESSED: {exc}")
        finally:
            ti.pid_is_ours = real_ours
        check("R3-M2 an OWNED pid file is kept", (root / "server.pid").exists())
    finally:
        ti._root_for = real_root_for
        for name in ("server.pid", "server.supervisor.pid", "server.port"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def r3l4_a_cim_query_failure_is_diagnosed() -> None:
    """R3-L4: `_command_line` returned "" for BOTH "this process has no matching command line"
    and "the query could not run" (no powershell, a sick CIM service, an AV hook). The second is
    a broken TOOL, and it fails closed into the M2 deadlock with a message blaming the process."""

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "Get-CimInstance : The RPC server is unavailable."

    class _Empty:
        returncode = 0
        stdout = ""
        stderr = ""

    root = Path(tempfile.gettempdir()) / "loctms_ti_nope"
    real_run, real_say = ti._run, ti._say
    said: list[str] = []
    ti._run = lambda argv, **kw: _Failed()
    ti._say = said.append
    try:
        cmdline, failure = ti._command_line(4242)
        _ours, why = ti.pid_is_ours(4242, root)
    finally:
        ti._run, ti._say = real_run, real_say
    check("R3-L4 a failed CIM query is reported as a failure", not cmdline and failure, failure)
    check("R3-L4 the diagnosis names the CIM query", "Get-CimInstance Win32_Process" in failure)
    check(
        "R3-L4 the operator is warned on stdout",
        any("could not read command lines" in m for m in said),
        str(said)[:200],
    )
    check("R3-L4 the refusal blames the tool, not the process", "CANNOT BE VERIFIED" in why, why)

    # ANTI-VACUITY: a query that RAN and returned nothing is a different verdict entirely.
    ti._run = lambda argv, **kw: _Empty()
    said2: list[str] = []
    ti._say = said2.append
    try:
        cmdline2, failure2 = ti._command_line(4242)
        _o2, why2 = ti.pid_is_ours(4242, root)
    finally:
        ti._run, ti._say = real_run, real_say
    check("R3-L4 an empty answer from a WORKING query is not a tool failure", failure2 == "")
    check("R3-L4 ... and warns about nothing", not said2 and not cmdline2, str(said2)[:200])
    check("R3-L4 ... and reads as a dead/unreadable pid", "no readable command line" in why2, why2)


def r2l4_a_surviving_listener_exits_non_zero() -> None:
    """R2-L4: when the port still has a listener after the kill loop, `down` must report failure.

    The loop is driven through fakes (netstat, taskkill, /health, sleep) so the branch is reached
    in milliseconds rather than by finding an unkillable process."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    port = 61999
    (root / "server.port").write_text(str(port), encoding="utf-8")
    # R4-M1: a root with no pid file at all is RETIRED and has no port to act on, so the orphan
    # shape this check drives needs its (empty) pid file -- exactly as on disk after a crash.
    (root / "server.pid").write_text("0", encoding="utf-8")
    real = (
        ti.port_owner_pids,
        ti._kill,
        ti._health_answers,
        ti.pid_is_ours,
        ti._sleep,
        ti._creation_date,
    )
    survivors = [4242]
    ti.port_owner_pids = lambda p: list(survivors)
    ti._kill = lambda pid: None  # a kill that does not work
    ti._health_answers = lambda p: True
    ti.pid_is_ours = lambda pid, r, p, trusted_parents=(): (True, "stub: ours")
    ti._sleep = lambda *_: None  # R3-L6: the module's indirection, not `time.sleep` itself
    ti._creation_date = lambda pid: "20260908010101.000000-000"  # a pid that does not turn over
    try:
        note, ok = ti.stop_server(root)
        check("R2-L4 a surviving listener is not success", not ok, note)
        check("R2-L4 the note says a listener remains", "still has a listener" in note, note)
        # ANTI-VACUITY: the identical path with a kill that WORKS reports ok.
        ti._kill = lambda pid: survivors.clear()
        note2, ok2 = ti.stop_server(root)
        check("R2-L4 a kill that works reports ok", ok2 and "no listener" in note2, note2)
    finally:
        (
            ti.port_owner_pids,
            ti._kill,
            ti._health_answers,
            ti.pid_is_ours,
            ti._sleep,
            ti._creation_date,
        ) = real
        for name in ("server.pid", "server.supervisor.pid", "server.port"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def r2m1_cmd_down_exit_code_follows_stop_server() -> None:
    """R2-M1/L4: the refusal must reach the SHELL, not just the console."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    real_root_for, real_stop, real_unreg = ti._root_for, ti.stop_server, ti.mcp_unregister
    ti._root_for = lambda name: root
    ti.mcp_unregister = lambda name: "(stubbed: no MCP client call from the suite)"
    try:
        ti.stop_server = lambda r, legacy_pid=False: ("WARNING: ... -- not killing it", False)
        check("R2-M1 cmd_down exits non-zero on a refusal", ti.cmd_down("verifyprobe") == 2)
        ti.stop_server = lambda r, legacy_pid=False: ("killed listener pid 1", True)
        check("R2-M1 cmd_down still exits 0 on a clean stop", ti.cmd_down("verifyprobe") == 0)
    finally:
        ti._root_for, ti.stop_server, ti.mcp_unregister = real_root_for, real_stop, real_unreg
        with contextlib.suppress(OSError):
            root.rmdir()


class _Tripwire(RuntimeError):
    """Raised by a stand-in for anything `cmd_up` does AFTER its refusals (round-2 gate L6)."""


@contextlib.contextmanager
def _no_side_effects():
    """Make a REGRESSED `cmd_up` guard fail the suite instead of building an instance.

    Round-2 gate L6: driving the real `cmd_up` is the right way to prove its refusals, but if a
    guard ever regresses the old test did not merely fail -- `cmd_up` ran on to a live engine
    preflight, an `rmtree` of `.test-instances/verifyprobe`, `alembic upgrade head`, a detached
    uvicorn, key minting and an `mcp add`, from inside pytest. Every door past the refusals is
    replaced by a tripwire, so a regression raises `_Tripwire` (a FAILED check) and touches
    nothing."""
    # `assert_data_dir_safe` is deliberately NOT tripwired: it is a refusal door, not a side
    # effect. `preflight_engines` is the first thing `cmd_up` does that reaches outside itself.
    # R3-L6: `_rmtree` is `test_instance`'s own indirection for `shutil.rmtree`, so the tripwire
    # is scoped to this module rather than patched into the shared stdlib `shutil`.
    saved = {
        name: getattr(ti, name)
        for name in ("preflight_engines", "cli", "start_server", "mcp_register", "_rmtree")
    }

    def _trip(*a, **kw):
        raise _Tripwire("cmd_up ran past its refusal and reached a real side effect")

    for name in saved:
        setattr(ti, name, _trip)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(ti, name, value)


def m2_up_refuses_a_busy_port() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    port = sock.getsockname()[1]
    try:
        with _no_side_effects():
            try:
                ti.cmd_up("verifyprobe", port)
                check("M2 up refuses a busy port", False, "cmd_up returned instead of aborting")
            except ti.Abort as exc:
                message = str(exc)
                check("M2 up refuses a busy port", "already in use" in message, message[:200])
            except _Tripwire as exc:
                check("M2 up refuses a busy port", False, f"GUARD REGRESSED: {exc}")
        # and it refused BEFORE writing anything
        check(
            "M2 the refusal happened before any instance root was created",
            not (ti.INSTANCES_ROOT / "verifyprobe").exists(),
        )
        # R2-L6 ANTI-VACUITY: the tripwires really do fire when the guard does not stop the run.
        with _no_side_effects():
            try:
                ti.cmd_up("verifyprobe", _free_port())
                check("R2-L6 the tripwire fires past the guards", False, "no _Tripwire")
            except _Tripwire:
                check("R2-L6 the tripwire fires past the guards", True)
            except ti.Abort as exc:
                check("R2-L6 the tripwire fires past the guards", False, f"aborted: {exc}"[:160])
        check(
            "R2-L6 the tripwired run created nothing either",
            not (ti.INSTANCES_ROOT / "verifyprobe").exists(),
        )
    finally:
        sock.close()


class _DeadChild:
    returncode = 3

    def poll(self):
        return 3


def m2_wait_health_fails_fast_on_a_dead_child() -> None:
    # A port that ANSWERS 200 while our own child is dead is somebody else's server.
    try:
        ti.wait_health(_free_port(), _DeadChild(), timeout=5.0)
        check("M2 wait_health aborts when the child died", False, "returned instead of aborting")
    except ti.Abort as exc:
        check(
            "M2 wait_health aborts when the child died", "exited rc=3" in str(exc), str(exc)[:200]
        )


# ---------------------------------------------------------------------------------------
# M3 -- no mock slot in the Advanced roster
# ---------------------------------------------------------------------------------------
class _CaptureApi:
    def __init__(self):
        self.body = None

    def jpost(self, path, body=None, **kw):
        self.body = body
        roster = (body or {}).get("model_roster") or {}
        return {"mission": {"id": 1, "model_roster": roster}}


def m3_roster_names_real_engines() -> None:
    api = _CaptureApi()
    seeder = ti.Seeder.__new__(ti.Seeder)
    seeder.api = api
    seeder.engine_set = "both"
    seeder.text_model = ti.LM_STUDIO_MODEL
    seeder.ids = {"document_short": 7, "engine_ollama_id": 2}
    out = seeder.api_create_advanced_mission(3)
    roster = (api.body or {}).get("model_roster") or {}
    committee = roster.get("committee") or {}
    slots = [*(committee.get("text") or []), committee.get("adjudicator") or {}]
    providers = [str((s or {}).get("provider") or "") for s in slots]
    check("M3 every committee slot names a provider", all(providers), str(providers))
    check("M3 no committee slot is mock", not any(p == "mock" for p in providers), str(providers))
    check("M3 the roster default provider is real", roster.get("provider") == "local", str(roster))
    check("M3 engine #2 carries the context-blind slot", "engine:2" in providers, str(providers))
    check(
        "M3 one text delegate is sighted (an all-blind roster is a 400)",
        any(not s.get("context_blind") for s in (committee.get("text") or [])),
    )
    check(
        "M3 the mission is recorded create-only",
        out.get("ran") is False and "CREATE ONLY" in out.get("note", ""),
    )

    # _mock_note must be DERIVED, not a constant: it flips when a mock slot appears.
    honest = ti.Seeder.__new__(ti.Seeder)
    honest.engine_set = "both"
    honest.ids = {"advanced_mission": {"committee": {"text": [{"provider": "local"}]}}}
    liar = ti.Seeder.__new__(ti.Seeder)
    liar.engine_set = "both"
    liar.ids = {"advanced_mission": {"committee": {"text": [{"provider": "mock"}]}}}
    check("M3 mock_note is clean when nothing uses mock", "WARNING" not in ti._mock_note(honest))
    check("M3 mock_note WARNS when something does", "WARNING" in ti._mock_note(liar))


# ---------------------------------------------------------------------------------------
# M4 -- a no-op generate is a skip, not a seed
# ---------------------------------------------------------------------------------------
class _GenApi:
    def __init__(self, payload):
        self.payload = payload

    def jpost(self, path, body=None, **kw):
        return self.payload


def m4_empty_generate_is_a_skip() -> None:
    seeder = ti.Seeder.__new__(ti.Seeder)
    seeder.api = _GenApi({"ok": True, "customer_id": 1, "scanned": 0, "proposed": 0})
    try:
        seeder._memory_suggestion(1)
        check("M4 a 0-proposed generate raises", False, "returned instead of aborting")
    except ti.Abort as exc:
        check(
            "M4 a 0-proposed generate raises", "proposed 0 suggestions" in str(exc), str(exc)[:160]
        )
    # ANTI-VACUITY: a generate that DID propose something is still a seed.
    seeder.api = _GenApi({"ok": True, "proposed": 2})
    check(
        "M4 a real generate is still a seed",
        seeder._memory_suggestion(1) == {"ok": True, "proposed": 2},
    )

    # the probe/seed split: a probe never lands in `ids`
    split = ti.Seeder.__new__(ti.Seeder)
    split.ids, split.probes, split.skipped = {}, {}, []
    split.step("worklist_probe", lambda: {"reachable": True}, probe=True)
    split.step("customer_1", lambda: 1)
    check(
        "M4 a probe is not filed as a seed",
        "worklist_probe" not in split.ids and "worklist_probe" in split.probes,
    )
    check("M4 a real seed still lands in ids", split.ids.get("customer_1") == 1)


# ---------------------------------------------------------------------------------------
# E1 / E2 / E3 -- the `--engines ollama` set must never touch LM Studio
# ---------------------------------------------------------------------------------------
def _ollama_seeder(api=None) -> "ti.Seeder":
    seeder = ti.Seeder.__new__(ti.Seeder)
    seeder.api = api
    seeder.env = {}
    seeder.engine_set = "ollama"
    seeder.text_model = ti.OLLAMA_MODEL
    seeder.ollama_base = ti.OLLAMA_HOSTS[0]
    seeder.ids, seeder.probes, seeder.skipped = {}, {}, []
    return seeder


def e1_ollama_set_never_touches_lm_studio() -> None:
    datadir = Path(tempfile.gettempdir()) / "nope"
    base = ti.OLLAMA_HOSTS[0]

    # -- the child environment: `local` IS the Ollama box, so every unspecified slot lands there
    env = ti.child_env(datadir, base, "ollama")
    check(
        "E1 LOCAL_BASE_URL is the Ollama box", env["LOCAL_BASE_URL"] == base, env["LOCAL_BASE_URL"]
    )
    check(
        "E1 the default local model is the Ollama model",
        env["DEFAULT_LOCAL_MODEL"] == ti.OLLAMA_MODEL,
    )
    check("E1 DEFAULT_PROVIDER is still the built-in `local`", env["DEFAULT_PROVIDER"] == "local")
    check(
        "E1 no LM Studio URL anywhere in the child env",
        ti.LM_STUDIO_URL not in "".join(env.values()),
    )
    check("E1 the engine set is recorded in the env", env["LOC_TMS_TEST_ENGINE_SET"] == "ollama")
    # E2 anti-vacuity: `both` is unchanged.
    both = ti.child_env(datadir, base, "both")
    check(
        "E2 --engines both still points `local` at LM Studio",
        both["LOCAL_BASE_URL"] == ti.LM_STUDIO_URL
        and both["DEFAULT_LOCAL_MODEL"] == ti.LM_STUDIO_MODEL,
        both["LOCAL_BASE_URL"],
    )

    # -- the preflight must not even PROBE LM Studio (the box may be off, or busy for somebody)
    probed: list[str] = []
    real_probe = ti._probe_json

    def _fake_probe(url, timeout=10.0):
        probed.append(url)
        if url.endswith("/api/tags"):
            return {"models": [{"name": ti.OLLAMA_MODEL}]}
        if url.endswith("/v1/models"):
            return {"data": [{"id": ti.OLLAMA_MODEL}]}
        raise AssertionError(f"unexpected probe {url}")

    ti._probe_json = _fake_probe
    try:
        lm_model, ollama_base, ollama_model = ti.preflight_engines("ollama")
    finally:
        ti._probe_json = real_probe
    check(
        "E1 preflight never calls LM Studio",
        not any(ti.LM_STUDIO_URL in u for u in probed),
        str(probed),
    )
    check(
        "E1 preflight checks the OpenAI-compatible surface the product uses",
        any(u.endswith("/v1/models") for u in probed),
        str(probed),
    )
    check("E1 preflight reports no LM Studio model", lm_model == "", lm_model)
    check(
        "E1 preflight resolves the Ollama box",
        ollama_base in ti.OLLAMA_HOSTS and ollama_model == ti.OLLAMA_MODEL,
    )

    # -- the engine ring: no LM Studio profile is created
    calls: list[tuple] = []
    real_cli = ti.cli
    ti.cli = lambda env, *args, **kw: calls.append(args) or ([] if kw.get("as_json") else None)
    try:
        _ollama_seeder()._engines()
    finally:
        ti.cli = real_cli
    created = [" ".join(str(a) for a in c) for c in calls if c[:2] == ("engine", "create")]
    check("E1 exactly one engine profile is created", len(created) == 1, str(created))
    check(
        "E1 no LM Studio engine profile", not any("LM Studio" in c for c in created), str(created)
    )
    check(
        "E1 the profile is the Ollama box",
        "Ollama" in created[0] and ti.OLLAMA_MODEL in created[0],
        created[0],
    )

    # -- the Advanced roster: every slot names the Ollama model / its engine ref, none is mock
    api = _CaptureApi()
    seeder = _ollama_seeder(api)
    seeder.ids = {"document_short": 7, "engine_ollama_id": 4}
    seeder.api_create_advanced_mission(3)
    roster = (api.body or {}).get("model_roster") or {}
    blob = str(roster)
    check("E1 the roster names no LM Studio model", ti.LM_STUDIO_MODEL not in blob, blob[:200])
    check(
        "E1 the roster's model is the Ollama model",
        roster.get("model") == ti.OLLAMA_MODEL,
        blob[:200],
    )
    check("E1 no roster slot is mock", '"mock"' not in blob and "'mock'" not in blob, blob[:200])

    # -- instance.md: the Engines section must not present LM Studio as wired
    facts = {
        "engine_set": "ollama",
        "ollama_base": base,
        "ollama_model": ti.OLLAMA_MODEL,
        "lm_model": "",
        "engines": "[]",
        "mock_note": ti._mock_note(_ollama_seeder()),
    }
    section = "\n".join(ti.engine_section(facts))
    check(
        "E1 instance.md says LM Studio is NOT part of the instance",
        "LM STUDIO IS NOT PART" in section,
    )
    check(
        "E1 instance.md does not call LM Studio the default",
        "Engine #1" not in section,
        section[:200],
    )
    check("E1 instance.md names the Ollama base as the default provider", base in section)
    check(
        "E1 the mock note is engine-set aware",
        "Ollama box" in facts["mock_note"] and "WARNING" not in facts["mock_note"],
        facts["mock_note"][:200],
    )
    # E2 anti-vacuity: the `both` section still describes two engines.
    both_section = "\n".join(
        ti.engine_section({**facts, "engine_set": "both", "lm_model": ti.LM_STUDIO_MODEL})
    )
    check(
        "E2 the both-set section still lists both engines",
        "Engine #1" in both_section and "Engine #2" in both_section,
        both_section[:200],
    )


def e3_unknown_engine_set_is_refused() -> None:
    # `preflight_engines` is checked directly; `cmd_up` is checked behind the R2-L6 tripwires, so
    # a regressed engine-set guard cannot build an instance from inside the suite. The unknown
    # name must be one no set is ever called: `lmstudio` was that name until 2026-09-16, when it
    # became a REAL set, so the probe name moved here.
    unknown = "gpu-farm"
    check("E3 the probe name is genuinely unknown", unknown not in ti.ENGINE_SETS)
    try:
        ti.preflight_engines(unknown)
        check("E3 preflight_engines refuses an unknown engine set", False, "no Abort")
    except ti.Abort as exc:
        check(
            "E3 preflight_engines refuses an unknown engine set",
            "unknown engine set" in str(exc),
            str(exc)[:160],
        )
    with _no_side_effects():
        try:
            ti.cmd_up("verifyprobe", _free_port(), unknown)
            check("E3 cmd_up refuses an unknown engine set", False, "no Abort")
        except ti.Abort as exc:
            check(
                "E3 cmd_up refuses an unknown engine set",
                "unknown engine set" in str(exc),
                str(exc)[:160],
            )
            # the refusal must NAME every set the tool knows, or the operator cannot recover
            check(
                "E3 the refusal names all three engine sets",
                all(s in str(exc) for s in ("both", "ollama", "lmstudio")),
                str(exc)[:160],
            )
        except _Tripwire as exc:
            check("E3 cmd_up refuses an unknown engine set", False, f"GUARD REGRESSED: {exc}")


# ---------------------------------------------------------------------------------------
# E4 -- the `--engines lmstudio` set must never touch the Ollama box (2026-09-16)
#
# The mirror image of E1. The box has been down at campaign time twice, and both existing sets
# require it; this set runs the instance on LM Studio ALONE. The box is never probed, never
# configured, never called and never presented as present -- there is simply no engine #2.
# ---------------------------------------------------------------------------------------
def _lmstudio_seeder(api=None) -> "ti.Seeder":
    seeder = ti.Seeder.__new__(ti.Seeder)
    seeder.api = api
    seeder.env = {}
    seeder.engine_set = "lmstudio"
    seeder.text_model = ti.LM_STUDIO_MODEL
    # preflight returns "" for the base under this set: nothing may assume it is a URL.
    seeder.ollama_base = ""
    seeder.ids, seeder.probes, seeder.skipped = {}, {}, []
    return seeder


def _mentions_the_box(blob: str) -> bool:
    hosts = [h.split("//", 1)[-1].split(":")[0] for h in ti.OLLAMA_HOSTS]
    return any(h in blob for h in (*ti.OLLAMA_HOSTS, *hosts))


def e4_lmstudio_set_never_touches_the_ollama_box() -> None:
    datadir = Path(tempfile.gettempdir()) / "nope"

    check("E4 `lmstudio` is an engine set", "lmstudio" in ti.ENGINE_SETS, str(ti.ENGINE_SETS))

    # -- the child environment: `local` is LM Studio, exactly as under `both`.
    # Round-7 gate MED-2: the `both` env here is built the way a LIVE `both` run builds it --
    # with a REAL Ollama base, which is what `preflight_engines("both")` hands `child_env`.
    # Passing "" to both calls made the identity check test nothing.
    env = ti.child_env(datadir, "", "lmstudio")
    both = ti.child_env(datadir, ti.OLLAMA_HOSTS[0], "both")
    check(
        "E4 LOCAL_BASE_URL is LM Studio",
        env["LOCAL_BASE_URL"] == ti.LM_STUDIO_URL,
        env["LOCAL_BASE_URL"],
    )
    check(
        "E4 the default local model is the LM Studio model",
        env["DEFAULT_LOCAL_MODEL"] == ti.LM_STUDIO_MODEL,
    )
    check("E4 DEFAULT_PROVIDER is still the built-in `local`", env["DEFAULT_PROVIDER"] == "local")
    check("E4 the engine set is recorded in the env", env["LOC_TMS_TEST_ENGINE_SET"] == "lmstudio")
    differing = {k for k in set(env) | set(both) if env.get(k) != both.get(k)}
    check(
        "E4 the child env differs from a LIVE `both` env in exactly the two informational keys",
        differing == {"LOC_TMS_TEST_ENGINE_SET", "LOC_TMS_TEST_OLLAMA_BASE"},
        str(sorted(differing)),
    )
    check(
        "E4 the two informational keys are the ONLY difference (the box base is blank, not a URL)",
        env["LOC_TMS_TEST_OLLAMA_BASE"] == "" and both["LOC_TMS_TEST_OLLAMA_BASE"] != "",
        f"{env['LOC_TMS_TEST_OLLAMA_BASE']!r} vs {both['LOC_TMS_TEST_OLLAMA_BASE']!r}",
    )
    check(
        "E4 no Ollama host anywhere in the child env",
        not _mentions_the_box("".join(env.values())),
    )
    # E2 anti-vacuity: the `both` env itself is unchanged.
    check(
        "E2 --engines both still points `local` at LM Studio (byte check)",
        both["LOCAL_BASE_URL"] == ti.LM_STUDIO_URL
        and both["DEFAULT_LOCAL_MODEL"] == ti.LM_STUDIO_MODEL
        and both["LOCAL_API_KEY"] == "lm-studio"
        and both["LOC_TMS_TEST_ENGINE_SET"] == "both",
        both["LOCAL_BASE_URL"],
    )

    # -- the preflight must not even PROBE the box (it is DOWN; that is the point of this set)
    probed: list[str] = []
    real_probe = ti._probe_json

    def _fake_probe(url, timeout=10.0):
        probed.append(url)
        if url.startswith(ti.LM_STUDIO_URL):
            return {"data": [{"id": ti.LM_STUDIO_MODEL}]}
        raise AssertionError(f"unexpected probe {url}")

    ti._probe_json = _fake_probe
    got = None
    try:
        got = ti.preflight_engines("lmstudio")
    except (ti.Abort, AssertionError) as exc:
        # Round-7 gate MED-1: this is the ANTI-VACUITY arm. It must fail as a CHECK, not as a
        # traceback that takes the rest of E4 with it -- a build without the set (or one that
        # still walks OLLAMA_HOSTS) has to be readable as a red line here.
        check("E4 preflight resolves the lmstudio set without touching the box", False, str(exc))
    finally:
        ti._probe_json = real_probe
    check(
        "E4 preflight never probes the Ollama box",
        probed and not any(_mentions_the_box(u) for u in probed),
        str(probed),
    )
    check("E4 preflight probed LM Studio", any(u.startswith(ti.LM_STUDIO_URL) for u in probed))
    check(
        "E4 preflight returns (lm model, '', '')",
        got == (ti.LM_STUDIO_MODEL, "", ""),
        str(got),
    )

    # -- and it must ABORT cleanly when LM Studio is down or does not serve the model
    def _dead_probe(url, timeout=10.0):
        raise OSError("connection refused")

    ti._probe_json = _dead_probe
    try:
        ti.preflight_engines("lmstudio")
        check("E4 preflight aborts when LM Studio is down", False, "no Abort")
    except ti.Abort as exc:
        check(
            "E4 preflight aborts when LM Studio is down",
            "not reachable" in str(exc) and not _mentions_the_box(str(exc)),
            str(exc)[:160],
        )
    except AssertionError as exc:  # pragma: no cover - a probe of the wrong host
        check("E4 preflight aborts when LM Studio is down", False, str(exc)[:160])
    finally:
        ti._probe_json = real_probe

    ti._probe_json = lambda url, timeout=10.0: {"data": [{"id": "some/other-model"}]}
    try:
        ti.preflight_engines("lmstudio")
        check("E4 preflight aborts when the model is not listed", False, "no Abort")
    except ti.Abort as exc:
        check(
            "E4 preflight aborts when the model is not listed",
            "does not list" in str(exc),
            str(exc)[:160],
        )
    finally:
        ti._probe_json = real_probe

    # -- the engine ring: exactly ONE profile (LM Studio) and no `engine_ollama_id`.
    # This is the second ANTI-VACUITY arm (round-7 gate MED-1): the old code created the Ollama
    # profile unconditionally, so `len(created) == 1` is red on a build without the set.
    calls: list[tuple] = []
    real_cli = ti.cli
    lm_row = {"id": 4, "name": "LM Studio (gemma-4-12b)", "kind": "local"}
    listed: list = [lm_row]

    def _fake_cli(env, *args, **kw):
        calls.append(args)
        return listed if kw.get("as_json") else None

    ti.cli = _fake_cli
    seeder = _lmstudio_seeder()
    ring: list = []
    try:
        ring = seeder._engines()
    except ti.Abort as exc:
        check("E4 the engine ring seeds cleanly under lmstudio", False, str(exc)[:160])
    finally:
        ti.cli = real_cli
    created = [" ".join(str(a) for a in c) for c in calls if c[:2] == ("engine", "create")]
    check("E4 exactly one engine profile is created", len(created) == 1, str(created))
    check(
        "E4 the profile is LM Studio and no profile names the box",
        bool(created) and "LM Studio" in created[0] and not any("Ollama" in c for c in created),
        str(created),
    )
    check(
        "E4 no engine_ollama_id is remembered",
        "engine_ollama_id" not in seeder.ids,
        str(seeder.ids),
    )
    check(
        "E4 the ring instance.md will print holds only the LM Studio profile",
        [r.get("name") for r in ring] == [lm_row["name"]],
        str(ring),
    )

    # -- round-7 gate L(a): a FOREIGN Ollama-shaped row in the ring contradicts the facts sheet's
    # "no second engine profile exists", so `_engines` must REFUSE rather than print both.
    listed = [
        lm_row,
        {"id": 9, "name": "Ollama box (gemma4:e4b-it-qat)", "kind": "openai_compatible"},
    ]
    ti.cli = _fake_cli
    stranger = _lmstudio_seeder()
    try:
        stranger._engines()
        check("E4 a foreign second profile is refused, not printed", False, "no Abort")
    except ti.Abort as exc:
        check(
            "E4 a foreign second profile is refused, not printed",
            "exactly ONE engine profile" in str(exc),
            str(exc)[:160],
        )
    finally:
        ti.cli = real_cli

    # -- the Advanced roster: every slot is LM Studio `local`, none is mock, none is an engine ref
    api = _CaptureApi()
    roster_seeder = _lmstudio_seeder(api)
    roster_seeder.ids = {"document_short": 7}
    roster_seeder.api_create_advanced_mission(3)
    roster = (api.body or {}).get("model_roster") or {}
    blob = str(roster)
    check("E4 the roster's model is the LM Studio model", roster.get("model") == ti.LM_STUDIO_MODEL)
    check(
        "E4 the roster default provider is `local`", roster.get("provider") == "local", blob[:200]
    )
    check("E4 no roster slot is an engine ref", "engine:" not in blob, blob[:200])
    check("E4 no roster slot is mock", '"mock"' not in blob and "'mock'" not in blob, blob[:200])

    # -- the engine-#2 MT proof is SKIPPED, honestly, and never runs
    def _skip_row(seed_ids: dict) -> tuple[list, str]:
        rows: list[tuple[str, str]] = []
        ti.cli = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("the lmstudio set ran the engine-#2 proof")
        )
        try:
            ti.proof_engine_two(
                _CaptureApi(),
                {},
                seed_ids,
                "lmstudio",
                lambda k, v: rows.append((k, v)),
            )
        finally:
            ti.cli = real_cli
        return rows, " | ".join(f"{k}: {v}" for k, v in rows)

    passed_ids = {
        "document_short": 7,
        "run_default_engine": {"run_id": 12, "status": "completed"},
    }
    recorded, joined = _skip_row(passed_ids)
    check("E4 the engine-#2 proof records exactly one line", len(recorded) == 1, joined)
    check(
        "E4 the engine-#2 proof is SKIPPED with the honest reason",
        "SKIPPED" in joined and "engine #2 absent (lmstudio set)" in joined,
        joined[:200],
    )
    check(
        "E4 the skipped proof is never recorded as ok",
        "-> ok" not in joined.lower() and "FAILED" not in joined,
        joined[:200],
    )
    check("E4 the skipped proof does not name the box", not _mentions_the_box(joined), joined[:200])
    check(
        "E4 a PASSED seeded run is reported as finished, from the recorded status",
        "run 12" in joined and "`completed`" in joined and "WARNING" not in joined,
        joined[:300],
    )

    # Reviewer round-1 MED: the row used to assert the seeded run "already proved end to end"
    # unconditionally. A run that FAILED, that was still running at the deadline, or that never
    # reached `ids` at all must not be published here as a success.
    for label, seed_ids, must_say in (
        ("failed", {"run_default_engine": {"run_id": 12, "status": "failed"}}, "`failed`"),
        ("timed out", {"run_default_engine": {"run_id": 12, "status": "running"}}, "`running`"),
        ("missing", {"document_short": 7}, "No seeded workflow run"),
    ):
        _, bad = _skip_row(seed_ids)
        check(
            f"E4 a {label} seeded run makes NO success claim in the skipped row",
            "proved" not in bad.lower()
            and "finished `" not in bad
            and "SKIPPED" in bad
            and must_say in bad,
            bad[:300],
        )
        check(
            f"E4 a {label} seeded run is still not recorded as ok, and never names the box",
            "-> ok" not in bad.lower() and not _mentions_the_box(bad),
            bad[:300],
        )

    # -- instance.md: engine #2 is declared ABSENT, not merely unmentioned
    facts = {
        "engine_set": "lmstudio",
        "ollama_base": "",
        "ollama_model": "",
        "lm_model": ti.LM_STUDIO_MODEL,
        "engines": "[]",
        "mock_note": ti._mock_note(_lmstudio_seeder()),
    }
    section = "\n".join(ti.engine_section(facts))
    check(
        "E4 instance.md names the engine set",
        "engine set: `lmstudio`" in section.lower(),
        section[:200],
    )
    check(
        "E4 instance.md declares engine #2 absent",
        "engine #2 absent" in section.lower(),
        section[:300],
    )
    check(
        "E4 instance.md never names the Ollama box",
        "Ollama" not in section and not _mentions_the_box(section),
        section[:300],
    )
    check(
        "E4 instance.md names LM Studio as the one engine",
        ti.LM_STUDIO_URL in section and ti.LM_STUDIO_MODEL in section,
        section[:300],
    )
    check(
        "E4 the mock note is engine-set aware and clean",
        "WARNING" not in facts["mock_note"]
        and "LM Studio" in facts["mock_note"]
        and "Ollama" not in facts["mock_note"],
        facts["mock_note"][:200],
    )


# ---------------------------------------------------------------------------------------
# L1 / L2 / L6
# ---------------------------------------------------------------------------------------
def l1_blanked_env() -> None:
    required = (
        "LOC_TMS_MOCK_FALLBACK",
        "DEFAULT_FALLBACK_PROVIDER",
        "LOC_TMS_SESSION_SECRET",
        "SESSION_SECRET",
        "LOC_TMS_ENCRYPTION_KEY",
        "ENCRYPTION_KEY",
    )
    missing = [v for v in required if v not in ti.BLANKED_ENV_VARS]
    check("L1 the fail-open + secret vars are blanked", not missing, str(missing))
    os.environ["LOC_TMS_MOCK_FALLBACK"] = "1"
    try:
        env = ti.child_env(Path(tempfile.gettempdir()) / "nope", "http://127.0.0.1:11434")
    finally:
        os.environ.pop("LOC_TMS_MOCK_FALLBACK", None)
    check("L1 an inherited LOC_TMS_MOCK_FALLBACK=1 is blanked", env["LOC_TMS_MOCK_FALLBACK"] == "")


def l2_no_shell_true() -> None:
    """No `shell=True` CALL anywhere (parsed, so the prose explaining the fix does not count)."""
    import ast

    tree = ast.parse((_ROOT / "scripts" / "test_instance.py").read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "shell" and getattr(kw.value, "value", False) is True
    ]
    check("L2 no shell=True call in the module", not offenders, f"lines {offenders}")


_FAKE_TOKEN = "TESTKEYtestkey0123456789abcdef"


def r2l1_mcp_stderr_is_scrubbed() -> None:
    """R2-L1: the MCP child gets LOC_STUDIO_TOKEN=<admin key>; its stderr reaches instance.md."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    real = ti._mcp_rpc
    ti._mcp_rpc = lambda *a, **kw: (
        None,
        f"Traceback: connecting with LOC_STUDIO_TOKEN={_FAKE_TOKEN} failed",
    )
    try:
        count, note = ti.mcp_tool_count(8399, _FAKE_TOKEN, root)
        called = ti.mcp_call_tool(8399, _FAKE_TOKEN, root, "run_report", {})
    finally:
        ti._mcp_rpc = real
        with contextlib.suppress(OSError):
            root.rmdir()
    check("R2-L1 the tools/list note carries no token", _FAKE_TOKEN not in note, note)
    check("R2-L1 the tools/call note carries no token", _FAKE_TOKEN not in called, called)
    # ANTI-VACUITY: the note is still a useful stderr tail, not blanked wholesale.
    check("R2-L1 the stderr tail survives the scrub", "failed" in note and count == -1, note)
    check("R2-L1 the scrub is visible", "***" in note, note)


def r2l2_down_scrubs_the_mcp_echo() -> None:
    """R2-L2: `down` echoes `mcp remove` output, and the removed entry carries the key."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    try:
        (root / "api-key.txt").write_text(_FAKE_TOKEN + "\n", encoding="utf-8")
        out = ti._scrub_with_instance_keys(
            f"removed loc-os-x (env LOC_STUDIO_TOKEN={_FAKE_TOKEN})", root
        )
        check("R2-L2 the MCP client echo carries no key", _FAKE_TOKEN not in out, out)
        check("R2-L2 the rest of the echo survives", "removed loc-os-x" in out, out)
        # ANTI-VACUITY: with no key file to read, the text is passed through unharmed rather than
        # silently blanked (a scrubber that blanks everything proves nothing).
        (root / "api-key.txt").unlink()
        plain = ti._scrub_with_instance_keys("removed loc-os-x", root)
        check("R2-L2 a keyless root does not mangle the echo", plain == "removed loc-os-x", plain)
    finally:
        for name in ("api-key.txt", "api-key-viewer.txt"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def mcp_cli_unset_is_a_clean_skip() -> None:
    """With LOC_TEST_MCP_CLI unset the MCP steps are skipped: no client is run, `up` carries on.

    `up` calls `mcp_register`, `mcp_tool_count` and `mcp_list`, and `down` calls
    `mcp_unregister`. None of them may spawn a process or raise when no client CLI is named,
    and the skip note must carry the equivalent stdio command WITHOUT the token."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    real_run, real_env = ti._run, os.environ.get(ti.MCP_CLI_ENV)
    spawned: list = []
    ti._run = lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("spawned"))
    try:
        os.environ.pop(ti.MCP_CLI_ENV, None)
        check("MCP-SKIP no client is resolved when the variable is unset", ti._mcp_cli() is None)
        note = ti.mcp_register("skipcase", 8123, _FAKE_TOKEN, root)
        check("MCP-SKIP register says skipped", note.startswith("skipped"), note)
        check("MCP-SKIP register prints the stdio command", "8123" in note and "mcp" in note, note)
        check("MCP-SKIP register never prints the token", _FAKE_TOKEN not in note, note)
        check("MCP-SKIP list is empty", ti.mcp_list() == "")
        check(
            "MCP-SKIP unregister says skipped", ti.mcp_unregister("skipcase").startswith("skipped")
        )
        check("MCP-SKIP nothing was spawned", spawned == [], str(spawned))
        # A blank value is the same as unset (an empty env var must not resolve to a client).
        os.environ[ti.MCP_CLI_ENV] = "   "
        check("MCP-SKIP a blank variable is unset", ti._mcp_cli() is None)
        # ANTI-VACUITY: a name that is not on PATH is a loud Abort, not a silent skip.
        os.environ[ti.MCP_CLI_ENV] = "no-such-mcp-client-9f3a"
        try:
            ti._mcp_cli()
            check("MCP-SKIP a named client that is missing aborts", False)
        except ti.Abort:
            check("MCP-SKIP a named client that is missing aborts", True)
    finally:
        ti._run = real_run
        if real_env is None:
            os.environ.pop(ti.MCP_CLI_ENV, None)
        else:
            os.environ[ti.MCP_CLI_ENV] = real_env
        shutil.rmtree(root, ignore_errors=True)


def r2l3_unresolved_port_owner_warns() -> None:
    """R2-L3: an empty netstat must WARN and be recorded, never silently alias the supervisor."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    real_owners, real_sleep = ti.port_owner_pids, ti._sleep
    ti.port_owner_pids = lambda p: []
    ti._sleep = lambda *_: None  # R3-L6: the module's indirection, not `time.sleep` itself
    try:
        listener, supervisor, note = ti.record_pids(root, 61998, _FakePopen(4242))
        check("R2-L3 the fallback still records the supervisor", listener == supervisor == 4242)
        check("R2-L3 the fallback is reported, not silent", "SUPERVISOR" in note, note)
        check("R2-L3 the note names the port", "61998" in note, note)
        # THE REFUSAL STILL APPLIES: our own child being dead means the /health 200 was somebody
        # else's server, and an unresolvable owner must not paper over that.
        try:
            ti.record_pids(root, 61998, _FakePopen(4242, alive=False))
            check("R2-L3 a dead child is still refused", False, "no Abort")
        except ti.Abort as exc:
            check("R2-L3 a dead child is still refused", "Refusing" in str(exc), str(exc)[:160])
        # ANTI-VACUITY: when netstat DOES answer, there is no note and no warning.
        ti.port_owner_pids = lambda p: [4242]
        _, _, quiet = ti.record_pids(root, 61998, _FakePopen(4242))
        check("R2-L3 a resolved owner is silent", quiet == "", quiet)
    finally:
        ti.port_owner_pids, ti._sleep = real_owners, real_sleep
        for name in ("server.pid", "server.supervisor.pid"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def r2l5_step_scrubs_its_reason() -> None:
    """R2-L5: the comment claimed a scrub; the code did not do one. The reason lands in
    instance.md's "Not seeded" table, and an API error body can quote the admin token."""

    class _TokenApi:
        _token = _FAKE_TOKEN

    seeder = ti.Seeder.__new__(ti.Seeder)
    seeder.api = _TokenApi()
    seeder.ids, seeder.probes, seeder.skipped = {}, {}, []

    def _boom():
        raise ti.Abort(f"POST /api/x -> 400 {{'echo': 'X-Loc-Token: {_FAKE_TOKEN}'}}")

    seeder.step("blows_up", _boom)
    reason = seeder.skipped[0][1]
    check("R2-L5 the skip reason carries no token", _FAKE_TOKEN not in reason, reason)
    check("R2-L5 the skip reason is still readable", "POST /api/x -> 400" in reason, reason)


def r2l8_start_server_rechecks_the_port() -> None:
    """R2-L8: the bind-test in `cmd_up` runs about a minute before the bind. Re-check at the
    bind itself."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    started: list[list] = []
    # R3-L6: `ti._popen` is `test_instance`'s own indirection. Patching it leaves the shared
    # stdlib `subprocess.Popen` (which `subprocess.run` is built on) untouched.
    real_popen = ti._popen

    def _fake_popen(*a, **kw):
        argv = list(a[0] if a else kw.get("args") or [])
        if any("uvicorn" in str(tok) for tok in argv):
            started.append(argv)
            return _FakePopen(4242)
        return real_popen(*a, **kw)

    ti._popen = _fake_popen
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    busy = sock.getsockname()[1]
    try:
        try:
            ti.start_server(root, busy, {})
            check("R2-L8 start_server refuses a port taken since the pre-check", False, "no Abort")
        except ti.Abort as exc:
            check(
                "R2-L8 start_server refuses a port taken since the pre-check",
                "taken between" in str(exc),
                str(exc)[:160],
            )
        check("R2-L8 nothing was launched", not started, str(started))
        check("R2-L8 no pid file was written", not (root / "server.pid").exists())
        # ANTI-VACUITY: on a free port it still starts and records, i.e. the re-check is a guard,
        # not a blanket refusal.
        sock.close()
        ti.start_server(root, busy, {})
        check("R2-L8 a free port still starts", len(started) == 1, str(started))
        check("R2-L8 the pid/port files are written", ti._read_pid(root / "server.port") == busy)
        # R3-M1: the argv it would have launched carries THIS root's instance stamp, inside the
        # `-c` snippet -- the only thing that makes `pid_is_ours` instance-unique in production.
        launched = " ".join(str(tok) for tok in (started[0] if started else []))
        check(
            "R3-M1 the launched argv carries this root's stamp inside the -c snippet",
            ti._stamp_for(root.resolve()) in launched and "-c" in launched,
            launched[:200],
        )
        check(
            "R3-M1 a DIFFERENT root's stamp is not in it",
            ti._stamp_for(Path(tempfile.gettempdir()) / "other") not in launched,
        )
    finally:
        ti._popen = real_popen
        with contextlib.suppress(Exception):
            sock.close()
        for name in ("server.pid", "server.supervisor.pid", "server.port", "server.log"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def _legacy_marker(port: int) -> str:
    """A comment line that makes a fake listener look like a PRE-STAMP instance of this tool.

    Round-4 H1: the shape is production's own uvicorn snippet (`ti._UVICORN_SNIPPET`), which is
    exactly what every instance started before the stamp carries on its command line -- a python
    running `app.main:app`, with no `# loc-test-instance:<root>#` anywhere."""
    return "# " + ti._UVICORN_SNIPPET.format(port=port)


def r4h1_a_live_legacy_instance_is_not_treated_as_a_stranger() -> None:
    """ROUND-4 H1, the destructive direction the round-3 deletion opened.

    `pid_is_ours` has three outcomes and the round-3 fix collapsed two of them: "provably NOT
    ours" fired for a recycled pid (where deleting the pid file is right) AND for our own live
    server whose command line simply predates the stamp. `down` then killed nothing, deleted both
    pid files -- the only record of what to kill by hand -- and printed "`up` can run again" while
    the server was still serving. The stand-in here is that server: a live python whose command
    line is the OLD uvicorn snippet, with no stamp."""
    if not IS_WINDOWS:
        print("  skip R4-H1-live (Windows-only process tooling)")
        return
    port = _free_port()
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_legacy_"))
    other = Path(tempfile.mkdtemp(prefix="loctms_ti_other_"))
    proc = _start_fake_server(port, _legacy_marker(port))
    stamped = None
    try:
        owners = ti.port_owner_pids(port)
        check("R4-H1 the legacy stand-in is listening", bool(owners), str(owners))
        legacy_pid = owners[0]
        cmdline, _f = ti._command_line(legacy_pid)
        check("R4-H1 it looks like a Loc OS uvicorn", ti.looks_like_loc_uvicorn(cmdline))
        check("R4-H1 and it carries no stamp at all", ti._STAMP_PREFIX not in cmdline)
        unstamped, why_not = ti.is_unstamped_loc_uvicorn(legacy_pid)
        check("R4-H1 is_unstamped_loc_uvicorn says yes", unstamped, why_not)
        # ANTI-VACUITY for that discriminator: this interpreter is a live python that does NOT
        # run `app.main:app`, so it is a stranger, not a legacy instance.
        check(
            "R4-H1 a live python that is not app.main:app is NOT legacy",
            not ti.is_unstamped_loc_uvicorn(os.getpid())[0],
            ti.is_unstamped_loc_uvicorn(os.getpid())[1],
        )

        # ARM 1: `down` keeps the files, kills nothing, and never says "`up` can run again".
        (root / "server.port").write_text(str(port), encoding="utf-8")
        (root / "server.pid").write_text(str(legacy_pid), encoding="utf-8")
        (root / "server.supervisor.pid").write_text("999998", encoding="utf-8")
        note, ok = ti.stop_server(root)
        check("R4-H1 down refuses a legacy instance", not ok, note)
        check("R4-H1 the refusal names it a LEGACY INSTANCE", "LEGACY INSTANCE" in note, note)
        check("R4-H1 the refusal names the --legacy-pid escape", "--legacy-pid" in note, note)
        check("R4-H1 the pid file was KEPT", (root / "server.pid").exists(), note)
        check("R4-H1 no file was called stale", "THAT FILE WAS DELETED" not in note, note)
        check("R4-H1 it never says `up` can run again", "can run again" not in note, note)
        check("R4-H1 the legacy server SURVIVED", ti._pid_alive(legacy_pid), note)
        check("R4-H1 it still owns the port", ti.port_owner_pids(port) == owners, note)
        # ANTI-VACUITY: the identical shape whose command line is NOT a Loc OS uvicorn is a
        # stranger -- that pid file really is stale and really is deleted.
        stranger_root = Path(tempfile.mkdtemp(prefix="loctms_ti_str_"))
        (stranger_root / "server.pid").write_text(str(os.getpid()), encoding="utf-8")
        note_s, ok_s = ti.stop_server(stranger_root)
        check("R4-H1 ANTI-VACUITY a true stranger's pid file is still deleted", not ok_s, note_s)
        check(
            "R4-H1 ANTI-VACUITY ... and is announced as stale",
            "THAT FILE WAS DELETED" in note_s and not (stranger_root / "server.pid").exists(),
            note_s,
        )
        check(
            "R4-H1 ANTI-VACUITY ... and the deletion note carries the pid",
            str(os.getpid()) in note_s,
            note_s,
        )
        with contextlib.suppress(OSError):
            stranger_root.rmdir()

        # ARM 2: `--legacy-pid` is REFUSED while /health does not answer on the recorded port.
        dead_port_root = Path(tempfile.mkdtemp(prefix="loctms_ti_dead_"))
        (dead_port_root / "server.port").write_text(str(_free_port()), encoding="utf-8")
        (dead_port_root / "server.pid").write_text(str(legacy_pid), encoding="utf-8")
        note2, ok2 = ti.stop_server(dead_port_root, legacy_pid=True)
        check("R4-H1 --legacy-pid is refused without a live /health", not ok2, note2)
        check("R4-H1 ... and says so", "--legacy-pid was REFUSED" in note2, note2)
        check("R4-H1 ... and killed nothing", ti._pid_alive(legacy_pid), note2)
        check("R4-H1 ... and kept the pid file", (dead_port_root / "server.pid").exists())
        for name in ("server.pid", "server.port"):
            (dead_port_root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            dead_port_root.rmdir()

        # ARM 3: a STAMPED instance of another root is never reachable, with or without the flag.
        # Round-5 H1 moved the refusal one rung EARLIER: a command line carrying a stamp that is
        # not this root's is now a STRANGER in `pid_is_ours` itself, so the LEGACY verdict (and
        # therefore the `--legacy-pid` precondition) is never even reached for it.
        stamped_port = _free_port()
        stamped = _start_fake_server(
            stamped_port, f"{_legacy_marker(stamped_port)}  {ti._stamp_for(other)}"
        )
        stamped_owners = ti.port_owner_pids(stamped_port)
        check("R4-H1 the stamped stand-in is listening", bool(stamped_owners), str(stamped_owners))
        stamped_root = Path(tempfile.mkdtemp(prefix="loctms_ti_stamped_"))
        (stamped_root / "server.port").write_text(str(stamped_port), encoding="utf-8")
        (stamped_root / "server.pid").write_text(str(stamped_owners[0]), encoding="utf-8")
        note3, ok3 = ti.stop_server(stamped_root, legacy_pid=True)
        check("R4-H1 --legacy-pid is refused over a STAMPED owner", not ok3, note3)
        check(
            "R4-H1 ... naming the stamp as the reason",
            "ANOTHER instance root's" in note3 and "stamp" in note3,
            note3,
        )
        check("R4-H1 ... and it is not called a LEGACY INSTANCE", "LEGACY INSTANCE" not in note3)
        check("R4-H1 ... and the other root's instance survived", ti._pid_alive(stamped_owners[0]))
        for name in ("server.pid", "server.port"):
            (stamped_root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            stamped_root.rmdir()

        # ARM 4 (the sanctioned teardown): /health answers and the owner is unstamped, so
        # `--legacy-pid` accepts the recorded pids for this one call and the server dies.
        note4, ok4 = ti.stop_server(root, legacy_pid=True)
        check("R4-H1 --legacy-pid does take down the legacy instance", ok4, note4)
        check("R4-H1 ... and says it was accepted", "--legacy-pid ACCEPTED" in note4, note4)
        check("R4-H1 ... and the port is free", not ti.port_owner_pids(port), note4)
        check("R4-H1 ... and the process is gone", not ti._pid_alive(legacy_pid), note4)
        left = [
            n for n in ("server.pid", "server.supervisor.pid", "server.port") if (root / n).exists()
        ]
        check("R4-H1 ... and the root is retired", not left, str(left))
    finally:
        for p in (proc, stamped):
            if p is not None and p.poll() is None:
                _kill_tree(p.pid)
        for name in ("server.pid", "server.supervisor.pid", "server.port"):
            (root / name).unlink(missing_ok=True)
        for r in (root, other):
            with contextlib.suppress(OSError):
                r.rmdir()


def r4h1_up_refuses_over_a_live_legacy_server() -> None:
    """R4-H1's escalation: with the pid files deleted, `up` on ANOTHER port walked straight into
    `_rmtree(root)` on a root whose datadir a live server holds open -- tearing out instance.md
    and the key files before failing on the locked SQLite file. Both doors are checked: a
    recorded pid that is a live legacy instance, and an `app.main:app` python on the RECORDED
    port (the case a bare `_pid_alive` never sees, because the recorded pid is long dead)."""
    if not IS_WINDOWS:
        print("  skip R4-H1-up-live (Windows-only process tooling)")
        return
    port = _free_port()
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_upleg_"))
    proc = _start_fake_server(port, _legacy_marker(port))
    real_root_for = ti._root_for
    ti._root_for = lambda name: root
    try:
        owners = ti.port_owner_pids(port)
        check("R4-H1 up: the legacy stand-in is listening", bool(owners), str(owners))
        (root / "server.port").write_text(str(port), encoding="utf-8")
        (root / "server.pid").write_text(str(owners[0]), encoding="utf-8")
        with _no_side_effects():
            try:
                ti.cmd_up("verifyprobe", _free_port())
                check("R4-H1 up refuses over a live legacy server", False, "no Abort")
            except ti.Abort as exc:
                message = str(exc)
                check(
                    "R4-H1 up refuses over a live legacy server",
                    "LEGACY" in message and "--legacy-pid" in message,
                    message[:240],
                )
            except _Tripwire as exc:
                check("R4-H1 up refuses over a live legacy server", False, f"REGRESSED: {exc}")
        check("R4-H1 up kept the legacy pid file", (root / "server.pid").exists())
        check("R4-H1 up killed nothing", ti._pid_alive(owners[0]))

        # The RECORDED-PORT door: the recorded pid is dead, so the pid loop sees nothing, but a
        # Loc OS uvicorn still owns the port this root recorded.
        (root / "server.pid").unlink(missing_ok=True)
        (root / "server.supervisor.pid").write_text("999999", encoding="utf-8")
        with _no_side_effects():
            try:
                ti.cmd_up("verifyprobe", _free_port())
                check("R4-H1 up refuses over the RECORDED port", False, "no Abort")
            except ti.Abort as exc:
                check(
                    "R4-H1 up refuses over the RECORDED port",
                    "RECORDED port" in str(exc),
                    str(exc)[:240],
                )
            except _Tripwire as exc:
                check("R4-H1 up refuses over the RECORDED port", False, f"REGRESSED: {exc}")

        # ANTI-VACUITY: with that server gone, the same root does NOT stop `up` -- it runs on to
        # the tripwires. (The pid file below names a pid that is now dead.)
        _kill_tree(proc.pid)
        for _ in range(40):
            if not ti.port_owner_pids(port):
                break
            time.sleep(0.25)
        with _no_side_effects():
            try:
                ti.cmd_up("verifyprobe", _free_port())
                check("R4-H1 ANTI-VACUITY up runs once the legacy server is gone", False, "no trip")
            except _Tripwire:
                check("R4-H1 ANTI-VACUITY up runs once the legacy server is gone", True)
            except ti.Abort as exc:
                check(
                    "R4-H1 ANTI-VACUITY up runs once the legacy server is gone",
                    False,
                    f"aborted: {exc}"[:200],
                )
    finally:
        ti._root_for = real_root_for
        if proc.poll() is None:
            _kill_tree(proc.pid)
        for name in ("server.pid", "server.supervisor.pid", "server.port"):
            (root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def r4m1_a_name_prefix_root_cannot_claim_another_instance() -> None:
    """ROUND-4 M1: the stamp was an UNTERMINATED substring, so every name-PREFIX root was a valid
    claimant of another instance's processes -- `.test-instances\\feat`'s marker was a substring
    of `...\\featoff`'s command line, so `down feat` would `taskkill /T` featoff's whole tree and
    exit 0. Roots `x` and `xy` here are the minimal shape of that pair."""
    parent = Path(tempfile.mkdtemp(prefix="loctms_ti_pfx_"))
    root_x = parent / "x"
    root_xy = parent / "xy"
    root_x.mkdir()
    root_xy.mkdir()
    # The emitter first: the string production actually launches.
    cmd_xy = ti.uvicorn_command(root_xy, 8350)
    check(
        "R4-M1 the prefix root's stamp does NOT match the longer root's command line",
        ti._stamp_for(root_x.resolve()) not in cmd_xy,
        cmd_xy[-90:],
    )
    check("R4-M1 the stamp is terminated on emit", cmd_xy.endswith(ti._STAMP_SUFFIX), cmd_xy[-90:])
    # ANTI-VACUITY: the root's OWN stamp still matches -- the delimiter did not break ownership.
    check(
        "R4-M1 ANTI-VACUITY the longer root's own stamp still matches",
        ti._stamp_for(root_xy.resolve()) in cmd_xy,
        cmd_xy[-90:],
    )
    check(
        "R4-M1 markers are compared as the full delimited value",
        all(
            m.endswith((ti._STAMP_SUFFIX, ti._STAMP_LEGACY_SUFFIX))
            for m in ti.instance_markers(root_x)
            if m.startswith("#")
        ),
        str(ti.instance_markers(root_x)),
    )
    if not IS_WINDOWS:
        print("  skip R4-M1-live (Windows-only process tooling)")
        return
    port = _free_port()
    proc = _start_fake_server(port, ti._stamp_for(root_xy))
    try:
        owners = ti.port_owner_pids(port)
        check("R4-M1 instance `xy` is listening", bool(owners), str(owners))
        (root_x / "server.port").write_text(str(port), encoding="utf-8")
        (root_x / "server.pid").write_text("999999", encoding="utf-8")
        note, ok = ti.stop_server(root_x)
        check("R4-M1 `down x` refuses `xy`'s process", not ok, note)
        check("R4-M1 `xy` survived", ti.port_owner_pids(port) == owners, note)
        # ANTI-VACUITY: `xy`'s own down kills it, through the identical arm.
        (root_xy / "server.port").write_text(str(port), encoding="utf-8")
        (root_xy / "server.pid").write_text("999999", encoding="utf-8")
        note2, ok2 = ti.stop_server(root_xy)
        check("R4-M1 ANTI-VACUITY `down xy` does kill it", ok2, note2)
        check("R4-M1 ANTI-VACUITY the port is free", not ti.port_owner_pids(port), note2)
    finally:
        if proc.poll() is None:
            _kill_tree(proc.pid)
        shutil.rmtree(parent, ignore_errors=True)


def r4m1_the_a7cd3494_stamp_is_still_provably_ours() -> None:
    """R4-M1 (version skew, both directions): an instance started by the `a7cd3494` build carries
    the UNTERMINATED stamp, whose terminator on the real command line is the closing quote of the
    `-c` argument (`... access_log=True)  # loc-test-instance:<root>"`). It must stay provably
    OURS -- otherwise the campaign's own instance could be stopped by neither the ordinary path
    (no `#`) nor `--legacy-pid` (it does carry a stamp) -- and it must NOT become a way for a
    name-prefix root to claim it.

    The command line is the real one, byte for byte: `_UVICORN_SNIPPET` plus the old stamp plus
    the quote Windows puts at the end of the `-c` argument. It is fed through `_command_line`
    rather than launched, because a literal `"` inside a `-c` snippet is escaped by the Windows
    quoting rules and so cannot reproduce this shape from a child process.
    """
    parent = Path(tempfile.mkdtemp(prefix="loctms_ti_skew_"))
    root_x = parent / "x"
    root_xy = parent / "xy"
    root_x.mkdir()
    root_xy.mkdir()
    old_cmdline = (
        f'{sys.executable} -c "{ti._UVICORN_SNIPPET.format(port=8350)}  '
        f'{ti._STAMP_PREFIX}{root_xy.resolve()}"'
    )
    check(
        "R4-M1 an a7cd3494 stamp still matches its own root",
        any(m in old_cmdline for m in ti.instance_markers(root_xy)),
        old_cmdline[-70:],
    )
    check(
        "R4-M1 ... and does NOT match the name-prefix root",
        not any(m in old_cmdline for m in ti.instance_markers(root_x)),
        str(ti.instance_markers(root_x))[:180],
    )
    real_cmdline = ti._command_line
    ti._command_line = lambda pid: (old_cmdline, "")
    try:
        ours, why = ti.pid_is_ours(4242, root_xy, 8350)
        check("R4-M1 pid_is_ours accepts the a7cd3494 stamp", ours, why)
        # ANTI-VACUITY: the prefix root cannot claim it. Round-4 read that as LEGACY INSTANCE
        # ("ours but unprovable"); round-5 H1 corrects it -- the command line carries a stamp, and
        # a stamp names a root, so for any OTHER root this is a provable STRANGER. That is what
        # keeps `--legacy-pid` from ever reaching it.
        prefix_ours, why_not = ti.pid_is_ours(4242, root_x, 8350)
        check("R4-M1 ANTI-VACUITY the prefix root still cannot claim it", not prefix_ours, why_not)
        check(
            "R5-H1 ANTI-VACUITY ... and it reads there as ANOTHER root's stamp, not LEGACY",
            "ANOTHER instance root's" in why_not and "LEGACY INSTANCE" not in why_not,
            why_not,
        )
    finally:
        ti._command_line = real_cmdline
        shutil.rmtree(parent, ignore_errors=True)


def r4m1_a_root_retired_by_an_earlier_build_is_disarmed() -> None:
    """R4-M1 (the disarm): roots retired by an EARLIER build kept `server.port` on disk, so they
    stayed live port-arm launchers pointing at a number some later instance would reuse. No pid
    file means no claim on any server, whatever `server.port`/instance.md still say, and `down`
    on such a root only unregisters MCP."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_retired_"))
    real_root_for, real_unreg = ti._root_for, ti.mcp_unregister
    removed: list[str] = []
    ti._root_for = lambda name: root
    ti.mcp_unregister = lambda name: removed.append(name) or "removed"
    try:
        (root / "server.port").write_text("8361\n", encoding="utf-8")
        (root / "instance.md").write_text(
            "- Name: `x`  ·  port: `8361`  ·  base URL: `http://127.0.0.1:8361`\n",
            encoding="utf-8",
        )
        check("R4-M1 a pre-existing retired root reports no port", ti._read_port(root) == 0)
        note, ok = ti.stop_server(root)
        check("R4-M1 down on a retired root is clean", ok, note)
        check("R4-M1 ... and says the root is RETIRED", "RETIRED" in note, note)
        check("R4-M1 ... and stopped nothing", "killed" not in note, note)
        rc = ti.cmd_down("verifyprobe")
        check("R4-M1 ... and only the MCP registration is removed", removed == ["verifyprobe"])
        check("R4-M1 ... exiting 0", rc == 0)
        # ANTI-VACUITY: put a pid file back and the root claims a server again -- `server.port`
        # resolves, and `stop_server` takes the real path instead of the retired one.
        (root / "server.pid").write_text("999999", encoding="utf-8")
        check(
            "R4-M1 ANTI-VACUITY a pid file re-arms the recorded port", ti._read_port(root) == 8361
        )
        real_owners = ti.port_owner_pids
        ti.port_owner_pids = lambda p: []
        try:
            note2, _ok2 = ti.stop_server(root)
        finally:
            ti.port_owner_pids = real_owners
        check("R4-M1 ANTI-VACUITY ... and down works the real path", "RETIRED" not in note2, note2)
    finally:
        ti._root_for, ti.mcp_unregister = real_root_for, real_unreg
        shutil.rmtree(root, ignore_errors=True)


def r4l1_the_port_arm_ignores_an_empty_second_read() -> None:
    """R4-L1: the port loop's re-check was `if born and _creation_date(pid) != born` -- the
    EMPTY second read (the owner simply exited, or one flaky CIM call) was missing from the
    condition, so a `down` that in fact ended with a free port exited 2 claiming the pid "was
    REPLACED". The pid loop already had the two-non-empty-readings rule; this is the same one."""
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_l1p_"))
    (root / "server.pid").write_text("0", encoding="utf-8")
    (root / "server.port").write_text("61998", encoding="utf-8")
    real = (
        ti.port_owner_pids,
        ti._kill,
        ti._health_answers,
        ti.pid_is_ours,
        ti._sleep,
        ti._creation_date,
    )
    killed: list[int] = []
    ti._kill = lambda pid: killed.append(pid)
    ti.port_owner_pids = lambda p: [] if killed else [4242]
    ti._health_answers = lambda p: True
    ti.pid_is_ours = lambda pid, r, p, trusted_parents=(): (True, "stub: ours")
    ti._sleep = lambda s: None
    try:
        reads = iter(["20260908010101.000000-000", ""])
        ti._creation_date = lambda pid: next(reads, "")
        note, ok = ti.stop_server(root)
        check("R4-L1 an empty second read is not a recycle", killed == [4242], str(killed))
        check("R4-L1 ... and nothing claims it was REPLACED", "REPLACED" not in note, note)
        check("R4-L1 ... and the down is a success", ok, note)
        # ANTI-VACUITY: two non-empty readings that DIFFER really are a recycled number, and the
        # kill is refused.
        killed.clear()
        (root / "server.pid").write_text("0", encoding="utf-8")
        (root / "server.port").write_text("61998", encoding="utf-8")
        reads2 = iter(["20260908010101.000000-000", "20260908999999.000000-000"])
        ti._creation_date = lambda pid: next(reads2, "20260908999999.000000-000")
        note2, ok2 = ti.stop_server(root)
        check("R4-L1 ANTI-VACUITY a real recycle is still refused", not killed, str(killed))
        check("R4-L1 ANTI-VACUITY ... and reported", "REPLACED" in note2, note2)
        check("R4-L1 ANTI-VACUITY ... and is not success", not ok2, note2)
    finally:
        (
            ti.port_owner_pids,
            ti._kill,
            ti._health_answers,
            ti.pid_is_ours,
            ti._sleep,
            ti._creation_date,
        ) = real
        shutil.rmtree(root, ignore_errors=True)


def r4l3_the_cim_failure_warning_is_latched() -> None:
    """R4-L3: `_command_line` printed its four-line warning on EVERY failed query, and the port
    loop asks up to 20 times per `down`, so a broken CIM service buried the notes. It is latched
    per command -- and the latch is RESET by each verb, so it never mutes a later run."""

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "Get-CimInstance : The RPC server is unavailable."

    root = Path(tempfile.mkdtemp(prefix="loctms_ti_l3_"))
    real = (ti._run, ti._say, ti._pid_alive, ti.port_owner_pids)
    said: list[str] = []
    ti._run = lambda argv, **kw: _Failed()
    ti._say = said.append
    ti._pid_alive = lambda pid: True
    ti.port_owner_pids = lambda p: []
    try:
        ti._cmdline_warning_latch(False)
        for _ in range(12):
            ti._command_line(4242)
        warned = [m for m in said if "could not read command lines" in m]
        check("R4-L3 twelve failed queries warn once", len(warned) == 1, str(len(warned)))
        # ANTI-VACUITY: the latch is per COMMAND -- a `down` resets it, so a broken CIM query is
        # never silently swallowed on a later run.
        (root / "server.pid").write_text("4242", encoding="utf-8")
        (root / "server.port").write_text("61997", encoding="utf-8")
        said.clear()
        note, ok = ti.stop_server(root)
        warned2 = [m for m in said if "could not read command lines" in m]
        check("R4-L3 ANTI-VACUITY the next command warns again", len(warned2) == 1, str(said)[:200])
        check("R4-L3 the unverifiable pid file is KEPT", (root / "server.pid").exists(), note)
        check("R4-L3 ... and the down is not a success", not ok, note)
    finally:
        (ti._run, ti._say, ti._pid_alive, ti.port_owner_pids) = real
        ti._cmdline_warning_latch(False)
        shutil.rmtree(root, ignore_errors=True)


_IDLE_PYTHON = "import time\ntime.sleep(600)\n"


def _start_idle_python(marker: str) -> subprocess.Popen:
    """A live python carrying `marker` on its command line that listens on NOTHING.

    Round-5 H1: the shape of a RECORDED pid that owns no port -- a legacy supervisor, or a
    `server.supervisor.pid` a reboot pointed at some other process. `--legacy-pid` proves things
    about the recorded PORT's owners, so it must never be spent on a pid like this."""
    proc = subprocess.Popen(
        [sys.executable, "-c", f"{_IDLE_PYTHON}{marker}\n"],
        creationflags=ti.DETACHED,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if ti._command_line(proc.pid)[0]:
            break
        time.sleep(0.25)
    return proc


def r5h1_legacy_pid_only_licenses_the_recorded_ports_own_unstamped_owner() -> None:
    """ROUND-5 H1: `--legacy-pid` checked the PORT OWNER and licensed the RECORDED PIDS.

    The round-4 build ran its `is_unstamped_loc_uvicorn` precondition against the current owners of
    the recorded port, stored the answer in one boolean, and then applied that boolean to whatever
    `server.pid` / `server.supervisor.pid` happened to name -- pids that were never checked and
    were never required to own that port. `pid_is_ours` compounded it by answering "LEGACY
    INSTANCE ... probably OURS" for ANY live `app.main:app` python, including one carrying another
    ROOT's stamp. A recorded pid that a reboot had left pointing at another root's proven, stamped,
    live instance was therefore `taskkill /T /F`ed, and `down` exited 0.

    The arm the round-4 suite missed is the one that matters: the port owner and the recorded pid
    must be DIFFERENT processes."""
    if not IS_WINDOWS:
        print("  skip R5-H1-live (Windows-only process tooling)")
        return
    port = _free_port()
    other_port = _free_port()
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_r5h1_"))
    other = Path(tempfile.mkdtemp(prefix="loctms_ti_r5oth_"))
    root2 = Path(tempfile.mkdtemp(prefix="loctms_ti_r5own_"))
    legacy = _start_fake_server(port, _legacy_marker(port))
    stamped = _start_fake_server(
        other_port, f"{_legacy_marker(other_port)}  {ti._stamp_for(other)}"
    )
    legacy2 = None
    idle = None
    try:
        owners = ti.port_owner_pids(port)
        stamped_owners = ti.port_owner_pids(other_port)
        check("R5-H1 the legacy stand-in is listening", bool(owners), str(owners))
        check("R5-H1 the other root's instance is listening", bool(stamped_owners), str(owners))
        legacy_owner = owners[0]
        stranger = stamped_owners[0]
        check("R5-H1 the two are different processes", legacy_owner != stranger, str(owners))

        # The verdict round 4 got wrong, checked directly.
        ours, why = ti.pid_is_ours(stranger, root, port)
        check("R5-H1 another root's stamped pid is not ours", not ours, why)
        check("R5-H1 ... it is named a STRANGER", "ANOTHER instance root's" in why, why)
        check("R5-H1 ... and NOT a LEGACY INSTANCE", "LEGACY INSTANCE" not in why, why)
        # ANTI-VACUITY: the very same pid is provably its OWN root's, so this is a discriminator,
        # not a blanket refusal of everything stamped.
        ours_own, why_own = ti.pid_is_ours(stranger, other, other_port)
        check("R5-H1 ANTI-VACUITY the stamped pid IS provably its own root's", ours_own, why_own)

        # ARM 1: recorded supervisor pid = another root's live stamped instance; the recorded
        # LISTENER pid is the (unstamped) owner of this root's recorded port. `--legacy-pid`.
        (root / "server.port").write_text(str(port), encoding="utf-8")
        (root / "server.pid").write_text(str(legacy_owner), encoding="utf-8")
        (root / "server.supervisor.pid").write_text(str(stranger), encoding="utf-8")
        note, ok = ti.stop_server(root, legacy_pid=True)
        check("R5-H1 the other root's instance SURVIVED", ti._pid_alive(stranger), note)
        check(
            "R5-H1 ... and still owns its own port",
            ti.port_owner_pids(other_port) == stamped_owners,
            note,
        )
        check("R5-H1 ... and the down is not a success", not ok, note)
        check("R5-H1 ... and the refusal names another root's stamp", "ANOTHER" in note, note)
        # ANTI-VACUITY (same call): the licence still reaches the recorded pid that really IS the
        # recorded port's unstamped owner, so the fix narrows the flag rather than disabling it.
        check(
            "R5-H1 ANTI-VACUITY the port's own unstamped owner was still killed",
            not ti._pid_alive(legacy_owner),
            note,
        )
        check("R5-H1 ANTI-VACUITY ... the port is free", not ti.port_owner_pids(port), note)
        check("R5-H1 ANTI-VACUITY ... and the licence was announced", "ACCEPTED" in note, note)

        # ARM 2: a recorded pid that IS an unstamped `app.main:app` python but owns no port at
        # all AND parents no owner of one (round-6 H1 widened the licence to a port owner's parent
        # supervisor; this stand-in is neither). The precondition passes (the recorded port's real
        # owner is unstamped and healthy), and the licence still may not be spent on it.
        port2 = _free_port()
        legacy2 = _start_fake_server(port2, _legacy_marker(port2))
        owners2 = ti.port_owner_pids(port2)
        check("R5-H1 the second legacy stand-in is listening", bool(owners2), str(owners2))
        idle = _start_idle_python(_legacy_marker(port2))
        unstamped, why_not = ti.is_unstamped_loc_uvicorn(idle.pid, root2)
        check("R5-H1 the idle pid is an unstamped app.main:app python", unstamped, why_not)
        check("R5-H1 ... that owns no port", idle.pid not in ti.port_owner_pids(port2))
        (root2 / "server.port").write_text(str(port2), encoding="utf-8")
        (root2 / "server.pid").write_text(str(idle.pid), encoding="utf-8")
        note2, ok2 = ti.stop_server(root2, legacy_pid=True)
        check("R5-H1 a recorded pid that owns no port is not licensed", ti._pid_alive(idle.pid))
        check(
            "R5-H1 ... and the refusal says exactly that",
            "neither an owner of this root's recorded port" in note2
            and "nor the parent of one" in note2,
            note2,
        )
        check("R5-H1 ... and its pid file was KEPT", (root2 / "server.pid").exists(), note2)
        check("R5-H1 ... and the down is not a success", not ok2, note2)
        check(
            "R5-H1 ... and the port's real owner was not killed either", ti._pid_alive(owners2[0])
        )
        # ANTI-VACUITY: record the port's OWN owner instead and the identical call kills it.
        (root2 / "server.port").write_text(str(port2), encoding="utf-8")
        (root2 / "server.pid").write_text(str(owners2[0]), encoding="utf-8")
        note3, ok3 = ti.stop_server(root2, legacy_pid=True)
        check("R5-H1 ANTI-VACUITY the port's own owner IS licensed", ok3, note3)
        check("R5-H1 ANTI-VACUITY ... and was killed", not ti._pid_alive(owners2[0]), note3)
        check(
            "R5-H1 ANTI-VACUITY ... and the idle pid was still not touched", ti._pid_alive(idle.pid)
        )
    finally:
        for p in (legacy, stamped, legacy2, idle):
            if p is not None and p.poll() is None:
                _kill_tree(p.pid)
        for r in (root, other, root2):
            shutil.rmtree(r, ignore_errors=True)


# The supervisor half of a legacy instance: it spawns the listener and then just sits there. The
# child is launched with `sys._base_executable` on purpose -- this repo's `.venv\Scripts\python.exe`
# is a wait-style redirector, so launching THROUGH it would insert a second process between the
# supervisor and the port owner and the pair would not be the shape `record_pids` records. The
# supervisor reports its own pid and the child's, because for the same reason the pid `Popen`
# returns here is the redirector, not this script.
_LEGACY_SUPERVISOR = """import os, subprocess, sys, time
exe = getattr(sys, "_base_executable", "") or sys.executable
child = subprocess.Popen([exe, "-c", sys.argv[1], sys.argv[2]])
with open(sys.argv[3], "w", encoding="utf-8") as fh:
    fh.write("%d %d" % (os.getpid(), child.pid))
time.sleep(600)
"""


def _start_legacy_pair(port: int, marker: str, out: Path) -> tuple[subprocess.Popen, int, int]:
    """The CANONICAL legacy instance: a SUPERVISOR that forked the process holding the port.

    Returns (the launcher Popen, the supervisor pid, the port owner's pid). uvicorn with
    `workers=1` runs exactly this shape -- `record_pids` exists because of it -- so a pre-stamp
    instance has TWO pid files, and the pid in `server.supervisor.pid` owns no port at all. What
    `record_pids` writes is a PARENT/CHILD pair (`:691`: a recorded listener is the recorded
    supervisor itself or `_parent_pid(listener) == supervisor`), and that is what this builds.
    Both processes carry the unstamped `app.main:app` command line and neither carries a stamp."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"{_LEGACY_SUPERVISOR}\n{marker}\n",
            f"{_FAKE_SERVER}\n{marker}\n",
            str(port),
            str(out),
        ],
        creationflags=ti.DETACHED,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    supervisor = worker = 0
    for _ in range(80):
        if ti.port_owner_pids(port) and out.exists():
            reported = out.read_text(encoding="utf-8").split()
            if len(reported) == 2:
                supervisor, worker = int(reported[0]), int(reported[1])
                break
        time.sleep(0.25)
    return proc, supervisor, worker


def r6h1_legacy_pid_licenses_the_port_owners_parent_supervisor() -> None:
    """ROUND-6 H1: `--legacy-pid` could not take down a legacy instance at all.

    The round-5 build licensed only a CURRENT OWNER of the recorded port. uvicorn `workers=1` forks
    the worker that holds the port, so the supervisor is in no `netstat` row, is never licensed, and
    survives -- and uvicorn's `keep_subprocess_alive` then respawns the worker on the same socket.
    The flag that exists for exactly one job could not do it. The licence now also reaches the
    PARENT of an owner, via the same `_parent_pid` relation `record_pids` uses.

    ARM 1 is the canonical shape (supervisor + worker, both recorded); ARM 2 is the anti-vacuity in
    ONE call -- the supervisor is licensed and killed while a sibling unstamped python that owns no
    port and parents no owner is refused by name."""
    if not IS_WINDOWS:
        print("  skip R6-H1-live (Windows-only process tooling)")
        return
    port = _free_port()
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_r6h1_"))
    root2 = Path(tempfile.mkdtemp(prefix="loctms_ti_r6sib_"))
    pair, supervisor, worker = _start_legacy_pair(port, _legacy_marker(port), root / "pair.txt")
    pair2 = None
    idle = None
    spawned: list[int] = [pair.pid, supervisor, worker]
    try:
        owners = ti.port_owner_pids(port)
        spawned += owners
        check("R6-H1 the legacy pair is listening", bool(owners), str(owners))
        check(
            "R6-H1 the port owner is the supervisor's child",
            worker in owners,
            f"worker={worker} owners={owners}",
        )
        check(
            "R6-H1 the supervisor is NOT the port owner",
            supervisor and supervisor not in owners,
            f"supervisor={supervisor} owners={owners}",
        )
        check(
            "R6-H1 ... but it IS the owner's parent, the relation record_pids records",
            ti._parent_pid(worker) == supervisor,
            f"parent={ti._parent_pid(worker)} supervisor={supervisor}",
        )
        sup_unstamped, sup_why = ti.is_unstamped_loc_uvicorn(supervisor, root)
        check("R6-H1 the supervisor is an unstamped app.main:app python", sup_unstamped, sup_why)

        # ARM 1: the real legacy shape -- `server.supervisor.pid` names the supervisor,
        # `server.pid` names the worker that owns the recorded port.
        (root / "server.port").write_text(str(port), encoding="utf-8")
        (root / "server.pid").write_text(str(worker), encoding="utf-8")
        (root / "server.supervisor.pid").write_text(str(supervisor), encoding="utf-8")
        note, ok = ti.stop_server(root, legacy_pid=True)
        check("R6-H1 the legacy teardown SUCCEEDS", ok, note)
        check(
            "R6-H1 ... the supervisor was licensed",
            f"ACCEPTED for pid {supervisor}" in note,
            note,
        )
        check("R6-H1 ... and named as the PARENT SUPERVISOR", "PARENT SUPERVISOR" in note, note)
        check("R6-H1 ... the supervisor is gone", not ti._pid_alive(supervisor), note)
        check("R6-H1 ... the worker is gone", not ti._pid_alive(worker), note)
        check("R6-H1 ... and the port is free", not ti.port_owner_pids(port), note)
        check(
            "R6-H1 ... and all three files are deleted",
            not any(
                (root / n).exists() for n in ("server.pid", "server.supervisor.pid", "server.port")
            ),
            note,
        )
        # The respawn window uvicorn's supervisor uses is 0.5 s; five seconds is ten of them.
        time.sleep(5)
        check("R6-H1 ... and nothing respawned on it", not ti.port_owner_pids(port), note)

        # ARM 2 (anti-vacuity, SAME call): a second legacy pair, but `server.pid` names a sibling
        # unstamped python that owns no port and is nobody's parent. The supervisor is still
        # licensed and killed; the sibling must be refused BY NAME and left alone.
        port2 = _free_port()
        pair2, supervisor2, worker2 = _start_legacy_pair(
            port2, _legacy_marker(port2), root2 / "pair.txt"
        )
        spawned += [pair2.pid, supervisor2, worker2]
        owners2 = ti.port_owner_pids(port2)
        spawned += owners2
        check("R6-H1 the second legacy pair is listening", worker2 in owners2, str(owners2))
        idle = _start_idle_python(_legacy_marker(port2))
        spawned.append(idle.pid)
        idle_unstamped, idle_why = ti.is_unstamped_loc_uvicorn(idle.pid, root2)
        check("R6-H1 the sibling is an unstamped app.main:app python", idle_unstamped, idle_why)
        check(
            "R6-H1 ... that owns no port and parents no owner",
            idle.pid not in owners2 and all(ti._parent_pid(o) != idle.pid for o in owners2),
            f"{idle.pid} vs {owners2}",
        )
        (root2 / "server.port").write_text(str(port2), encoding="utf-8")
        (root2 / "server.pid").write_text(str(idle.pid), encoding="utf-8")
        (root2 / "server.supervisor.pid").write_text(str(supervisor2), encoding="utf-8")
        note2, ok2 = ti.stop_server(root2, legacy_pid=True)
        check("R6-H1 ANTI-VACUITY the sibling was NOT licensed", ti._pid_alive(idle.pid), note2)
        check(
            "R6-H1 ANTI-VACUITY ... and the refusal says exactly why",
            "neither an owner of this root's recorded port" in note2
            and "nor the parent of one" in note2,
            note2,
        )
        check("R6-H1 ANTI-VACUITY ... and its pid file was KEPT", (root2 / "server.pid").exists())
        check("R6-H1 ANTI-VACUITY ... and the down is not a success", not ok2, note2)
        check(
            "R6-H1 ANTI-VACUITY ... while the supervisor WAS licensed and killed",
            f"ACCEPTED for pid {supervisor2}" in note2 and not ti._pid_alive(supervisor2),
            note2,
        )
        check("R6-H1 ANTI-VACUITY ... taking its worker with it", not ti._pid_alive(worker2), note2)
    finally:
        # Only pids this test spawned itself (the two supervisors, their workers and the sibling).
        for pid in spawned:
            if ti._pid_alive(pid):
                _kill_tree(pid)
        for r in (root, root2):
            shutil.rmtree(r, ignore_errors=True)


def r5m1_a_stray_legacy_pid_flag_is_a_plain_down() -> None:
    """ROUND-5 M1: a stray `--legacy-pid` on a HEALTHY modern instance killed it and then reported
    failure. The precondition was evaluated eagerly, `is_unstamped_loc_uvicorn` refuses a stamped
    owner, and this root's own stamp IS a stamp -- so the flag was always "REFUSED", `ok` went
    False, all three pid files were kept and the MCP registration was left pointing at a server
    that was already dead. `cmd_up` tells the operator to add the flag when unsure, so this is a
    typed path, not an accident."""
    if not IS_WINDOWS:
        print("  skip R5-M1-live (Windows-only process tooling)")
        return
    real = (ti._root_for, ti.mcp_unregister, ti._say)
    removed: list[str] = []
    said: list[str] = []
    procs: list[subprocess.Popen] = []
    roots: list[Path] = []
    ti.mcp_unregister = lambda name: removed.append(name) or "removed"
    ti._say = said.append
    try:
        outcomes = {}
        for label, flag in (("plain", False), ("stray-flag", True)):
            port = _free_port()
            root = Path(tempfile.mkdtemp(prefix="loctms_ti_r5m1_"))
            roots.append(root)
            ti._root_for = lambda name, _r=root: _r
            # A HEALTHY MODERN instance: production's own uvicorn snippet AND this root's stamp.
            proc = _start_fake_server(port, f"{_legacy_marker(port)}  {ti._stamp_for(root)}")
            procs.append(proc)
            owner = (ti.port_owner_pids(port) or [0])[0]
            (root / "server.port").write_text(str(port), encoding="utf-8")
            (root / "server.pid").write_text(str(owner), encoding="utf-8")
            if label == "stray-flag":
                # ROUND-5 L1: the refusal text used to assert "and it is not this root's" about
                # this root's OWN stamp. Both directions, on the same live process.
                _, mine = ti.is_unstamped_loc_uvicorn(owner, root)
                check(
                    "R5-L1 this root's own stamp is named as its own",
                    "THIS root's own" in mine,
                    mine,
                )
                _, theirs = ti.is_unstamped_loc_uvicorn(owner, Path(tempfile.gettempdir()) / "nope")
                check(
                    "R5-L1 ANTI-VACUITY another root sees it as ANOTHER's",
                    "ANOTHER" in theirs,
                    theirs,
                )
            removed.clear()
            said.clear()
            rc = ti.cmd_down("r5m1probe", legacy_pid=flag)
            left = [
                n
                for n in ("server.pid", "server.supervisor.pid", "server.port")
                if (root / n).exists()
            ]
            outcomes[label] = (rc, left, list(removed), "\n".join(said), owner)
        for label in ("plain", "stray-flag"):
            rc, left, unreg, out, owner = outcomes[label]
            check(f"R5-M1 [{label}] down exits 0", rc == 0, out[:240])
            check(f"R5-M1 [{label}] all three files are removed", not left, str(left))
            check(f"R5-M1 [{label}] MCP was unregistered", unreg == ["r5m1probe"], str(unreg))
            check(f"R5-M1 [{label}] the server is gone", not ti._pid_alive(owner), out[:240])
        stray_out = outcomes["stray-flag"][3]
        check("R5-M1 the stray flag is never REFUSED", "REFUSED" not in stray_out, stray_out[:300])
        check("R5-M1 ... and never ACCEPTED either", "ACCEPTED" not in stray_out, stray_out[:300])
        check(
            "R5-M1 ... it reads exactly like the plain down",
            "killed listener pid" in stray_out and "killed listener pid" in outcomes["plain"][3],
            stray_out[:300],
        )

        # ANTI-VACUITY: the precondition is LAZY, not dead. On a root that really does reach a
        # LEGACY verdict it is still evaluated, announced and spent.
        port = _free_port()
        root = Path(tempfile.mkdtemp(prefix="loctms_ti_r5m1l_"))
        roots.append(root)
        ti._root_for = lambda name, _r=root: _r
        proc = _start_fake_server(port, _legacy_marker(port))
        procs.append(proc)
        owner = (ti.port_owner_pids(port) or [0])[0]
        (root / "server.port").write_text(str(port), encoding="utf-8")
        (root / "server.pid").write_text(str(owner), encoding="utf-8")
        removed.clear()
        said.clear()
        rc = ti.cmd_down("r5m1probe", legacy_pid=True)
        out = "\n".join(said)
        check(
            "R5-M1 ANTI-VACUITY a real legacy root still evaluates the precondition", rc == 0, out
        )
        check("R5-M1 ANTI-VACUITY ... and announces the licence", "ACCEPTED" in out, out[:300])
        check("R5-M1 ANTI-VACUITY ... and the legacy server is gone", not ti._pid_alive(owner), out)
    finally:
        (ti._root_for, ti.mcp_unregister, ti._say) = real
        for p in procs:
            if p.poll() is None:
                _kill_tree(p.pid)
        for r in roots:
            shutil.rmtree(r, ignore_errors=True)


def r5m2_up_refuses_when_only_server_port_survives() -> None:
    """ROUND-5 M2: `cmd_up`'s recorded-port guard was disabled in exactly the state the tool's own
    ESCAPE HATCH creates. `_read_port` returns 0 without a pid file (round-4 M1's disarm, right for
    `down`'s port arm), and the hatch -- printed on the CANNOT-BE-VERIFIED path, which fires
    precisely when the server may still be serving -- tells the operator to delete the pid files by
    hand. `up <name> <other port>` then walked into `_rmtree(root)` over a live server."""
    if not IS_WINDOWS:
        print("  skip R5-M2-live (Windows-only process tooling)")
        return
    port = _free_port()
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_r5m2_"))
    proc = _start_fake_server(port, _legacy_marker(port))
    real_root_for = ti._root_for
    ti._root_for = lambda name: root
    try:
        (root / "server.port").write_text(str(port), encoding="utf-8")
        check(
            "R5-M2 the hand-cleared root has no pid file",
            not any((root / n).exists() for n in ("server.pid", "server.supervisor.pid")),
        )
        check(
            "R5-M2 _read_port still gives it none (down stays disarmed)", ti._read_port(root) == 0
        )
        with _no_side_effects():
            try:
                ti.cmd_up("verifyprobe", _free_port())
                check("R5-M2 up refuses over the live recorded port", False, "no Abort")
            except ti.Abort as exc:
                check(
                    "R5-M2 up refuses over the live recorded port",
                    "RECORDED port" in str(exc),
                    str(exc)[:240],
                )
            except _Tripwire as exc:
                check("R5-M2 up refuses over the live recorded port", False, f"REGRESSED: {exc}")
        check("R5-M2 up killed nothing", ti._pid_alive(proc.pid))
        check("R5-M2 up deleted nothing", (root / "server.port").exists())
        # ANTI-VACUITY: free the port and the identical root does NOT stop `up` -- it runs on to
        # the tripwires, so the guard is a guard and not a blanket refusal.
        _kill_tree(proc.pid)
        for _ in range(40):
            if not ti.port_owner_pids(port):
                break
            time.sleep(0.25)
        with _no_side_effects():
            try:
                ti.cmd_up("verifyprobe", _free_port())
                check("R5-M2 ANTI-VACUITY up runs once the port is free", False, "no trip")
            except _Tripwire:
                check("R5-M2 ANTI-VACUITY up runs once the port is free", True)
            except ti.Abort as exc:
                check(
                    "R5-M2 ANTI-VACUITY up runs once the port is free", False, f"abort: {exc}"[:200]
                )
    finally:
        ti._root_for = real_root_for
        if proc.poll() is None:
            _kill_tree(proc.pid)
        shutil.rmtree(root, ignore_errors=True)


def r5l2_status_reports_the_port_of_a_hand_cleared_root() -> None:
    """ROUND-5 L2: after the escape hatch, `status` -- the one command an operator runs to find out
    whether the server is still up -- printed `listener pid: (none/unreadable)` and no `/health`
    line at all, because it keyed the whole block off `_read_port`. Reporting is not acting."""
    if not IS_WINDOWS:
        print("  skip R5-L2-live (Windows-only process tooling)")
        return
    port = _free_port()
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_r5l2_"))
    proc = _start_fake_server(port, _legacy_marker(port))
    real = (ti._root_for, ti.mcp_list, ti._say)
    said: list[str] = []
    ti._root_for = lambda name: root
    ti.mcp_list = lambda: ""
    ti._say = said.append
    try:
        owner = (ti.port_owner_pids(port) or [0])[0]
        (root / "server.port").write_text(str(port), encoding="utf-8")
        rc = ti.cmd_status("r5l2probe")
        out = "\n".join(said)
        check("R5-L2 status still returns 0", rc == 0, out)
        check("R5-L2 it names the recorded port", f"port {port}" in out, out)
        check("R5-L2 ... labelled as recorded-without-a-pid", "hand-cleared" in out, out)
        check(
            "R5-L2 ... with the live owner pid", bool(owner) and str(owner) in out, f"{owner}|{out}"
        )
        check("R5-L2 ... and a /health answer", "/health: 200" in out, out)
        # ANTI-VACUITY: a genuinely retired root (no `server.port` either) still prints no port
        # block at all -- the fallback reads a recorded file, it does not invent a port.
        said.clear()
        (root / "server.port").unlink()
        ti.cmd_status("r5l2probe")
        out2 = "\n".join(said)
        check(
            "R5-L2 ANTI-VACUITY a retired root prints no port block",
            "listener pid(s)" not in out2 and "/health" not in out2,
            out2,
        )
    finally:
        (ti._root_for, ti.mcp_list, ti._say) = real
        if proc.poll() is None:
            _kill_tree(proc.pid)
        shutil.rmtree(root, ignore_errors=True)


def l6_junk_pid_file_reads_as_zero() -> None:
    root = Path(tempfile.mkdtemp(prefix="loctms_ti_"))
    try:
        (root / "server.pid").write_text("not-a-pid\n", encoding="utf-8")
        check("L6 a junk pid file reads as 0", ti._read_pid(root / "server.pid") == 0)
        check("L6 a missing pid file reads as 0", ti._read_pid(root / "nope.pid") == 0)
        (root / "server.pid").write_text(" 4242 \n", encoding="utf-8")
        check("L6 a real pid file still parses", ti._read_pid(root / "server.pid") == 4242)
    finally:
        (root / "server.pid").unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()


def l6_the_suite_left_the_stdlib_alone() -> None:
    """R3-L6: no test in this file may leave a SHARED stdlib module attribute patched.

    Every stand-in above targets `test_instance`'s own module-level indirection (`ti._run`,
    `ti._popen`, `ti._sleep`, `ti._rmtree`). `ti.shutil`/`ti.time`/`ti.subprocess` are the same
    module objects every other test in the interpreter imports, so patching THEM is global for
    the duration -- contained today only because this suite runs as its own subprocess. This
    check makes the containment an assertion rather than an accident, and it runs LAST so it
    sees whatever the rest of the file left behind."""
    check("R3-L6 stdlib subprocess.run is untouched", subprocess.run is ti.subprocess.run)
    check("R3-L6 stdlib subprocess.Popen is untouched", subprocess.Popen is ti.subprocess.Popen)
    check("R3-L6 stdlib time.sleep is untouched", time.sleep is ti.time.sleep)
    check("R3-L6 stdlib shutil.rmtree is untouched", shutil.rmtree is ti.shutil.rmtree)
    # ANTI-VACUITY: the module's own indirections are what the suite patches, and they are
    # restored too -- they still point at the stdlib functions they were built from.
    check("R3-L6 ti._run is restored", ti._run is subprocess.run)
    check("R3-L6 ti._popen is restored", ti._popen is subprocess.Popen)
    check("R3-L6 ti._sleep is restored", ti._sleep is time.sleep)
    check("R3-L6 ti._rmtree is restored", ti._rmtree is shutil.rmtree)


def h1_ollama_hosts_come_from_the_environment_only():
    """No private host is baked in: hosts are parsed from LOC_TMS_TEST_OLLAMA_HOSTS, and
    `up --engines ollama|both` with none configured aborts with a pointer to the variable."""
    src = (_ROOT / "scripts" / "test_instance.py").read_text(encoding="utf-8")
    check("h1 no private address literal", "100.65." not in src and "192.168." not in src)
    check(
        "h1 hosts parsed from env",
        ti.OLLAMA_HOSTS == ("http://192.0.2.10:11434", "http://192.0.2.11:11434"),
        str(ti.OLLAMA_HOSTS),
    )
    saved = ti.OLLAMA_HOSTS
    ti.OLLAMA_HOSTS = ()
    try:
        raised = ""
        try:
            ti.preflight_engines("ollama")
        except ti.Abort as exc:
            raised = str(exc)
        check("h1 empty hosts aborts", "LOC_TMS_TEST_OLLAMA_HOSTS" in raised, raised)
    finally:
        ti.OLLAMA_HOSTS = saved


def main() -> int:
    print("verify_test_instance: scripts/test_instance.py round-1 gate fixes")
    h1_failing_cli_message_has_no_password()
    m1_down_kills_the_real_listener()
    r2m1_down_refuses_a_listener_that_is_not_ours()
    r3m1_a_second_instance_on_the_same_port_survives_a_retired_down()
    r3m1_a_retired_root_has_no_port_to_act_on()
    r3l1_identity_is_reasserted_before_the_kill()
    r2l4_a_surviving_listener_exits_non_zero()
    r2m1_cmd_down_exit_code_follows_stop_server()
    r3l2_a_refused_down_keeps_the_mcp_registration()
    r3m2_up_is_not_deadlocked_by_a_recycled_pid()
    r3l4_a_cim_query_failure_is_diagnosed()
    r4h1_a_live_legacy_instance_is_not_treated_as_a_stranger()
    r4h1_up_refuses_over_a_live_legacy_server()
    r4m1_a_name_prefix_root_cannot_claim_another_instance()
    r4m1_the_a7cd3494_stamp_is_still_provably_ours()
    r4m1_a_root_retired_by_an_earlier_build_is_disarmed()
    r4l1_the_port_arm_ignores_an_empty_second_read()
    r4l3_the_cim_failure_warning_is_latched()
    r5h1_legacy_pid_only_licenses_the_recorded_ports_own_unstamped_owner()
    r6h1_legacy_pid_licenses_the_port_owners_parent_supervisor()
    r5m1_a_stray_legacy_pid_flag_is_a_plain_down()
    r5m2_up_refuses_when_only_server_port_survives()
    r5l2_status_reports_the_port_of_a_hand_cleared_root()
    m2_up_refuses_a_busy_port()
    m2_wait_health_fails_fast_on_a_dead_child()
    m3_roster_names_real_engines()
    m4_empty_generate_is_a_skip()
    e1_ollama_set_never_touches_lm_studio()
    e3_unknown_engine_set_is_refused()
    e4_lmstudio_set_never_touches_the_ollama_box()
    h1_ollama_hosts_come_from_the_environment_only()
    l1_blanked_env()
    l2_no_shell_true()
    r2l1_mcp_stderr_is_scrubbed()
    r2l2_down_scrubs_the_mcp_echo()
    mcp_cli_unset_is_a_clean_skip()
    r2l3_unresolved_port_owner_warns()
    r2l5_step_scrubs_its_reason()
    r2l8_start_server_rechecks_the_port()
    l6_junk_pid_file_reads_as_zero()
    l6_the_suite_left_the_stdlib_alone()
    if FAILURES:
        print("\n".join(f"FAILED: {f}" for f in FAILURES), file=sys.stderr)
        return 1
    print("TEST-INSTANCE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
