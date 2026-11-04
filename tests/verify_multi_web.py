"""S3c multi-process web (ADR 0097): N web processes are a supported layout.

Real processes on one throwaway LOC_TMS_DATA_DIR (SQLite): `uvicorn --workers 2` with
LOC_TMS_ROLE=web and LOC_TMS_WEB_PROCESSES=2, plus one `python -m app.worker`. Offline (mock
engine), no network beyond 127.0.0.1.

  [A] LOC_TMS_WEB_PROCESSES above 1 is refused in the single `all` role (N copies of the
      single process would each run the job pools and the scheduler).
  [B] The boot lock serializes the boots of several processes (no two inside at once).
  [C] Boot cleanups are owner-aware: a web process's boot fails the background /api calls and
      live translate streams of web processes that are GONE (and ownerless rows, the old
      rule), never the rows a LIVE peer web process is still serving.
  [D] Two live web processes + a worker: both boot, a translate job queued through the web
      processes runs on the worker and every poll (new connections, so both web processes
      answer) sees it complete.
  [E] The /api rate limit holds across the web processes: with a 40/minute read budget, 90
      requests on fresh connections get at most 40 answers, not 40 per process.
  [F] Keep-alive connections are rebalanced: some responses on one kept-alive connection carry
      `Connection: close` in the multi-web layout, none in the single process.

NEW BEHAVIOUR vs the pre-S3c tree: [A] config accepts the variable silently there, [B] there
is no boot lock, [C] the boot fails a live peer's rows, [E] each process kept its own window
(up to 80 answers), [F] no rebalancing.
"""

import contextlib
import http.client
import itertools
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
os.environ["LOC_TMS_ROLE"] = "web"
from tests._bootstrap import init_env

DATA = init_env("loctms_multiweb_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Job, JobStatus, Role
from app.services import apikeys, jobs, leader, security, settings
from app.web import background_calls

PY = sys.executable
READ_LIMIT = 40
FIXTURE_PASSWORD = "offline-fixture-password-123"  # pregate: allow-secret offline fixture
PROCS = []


def _fail(msg):
    raise AssertionError(msg)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# POSIX: each long-lived child leads its own process group, so _kill takes its whole tree.
# `uvicorn --workers 2` spawns its workers through multiprocessing; killing the supervisor
# alone left them orphaned for good (2 leaked per run on a test server, 2026-10-28). setpgrp keeps
# the caller's session, so the suite wrapper's session sweep still finds them.
_GROUP = {} if os.name == "nt" else {"preexec_fn": os.setpgrp}


def _kill(proc):
    if proc.poll() is not None and os.name == "nt":
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        # Even an exited leader's group can still hold its workers.
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(15)


def _child_env(**extra):
    env = dict(os.environ, LOC_TMS_DATA_DIR=DATA, PYTHONUTF8="1")
    env.update(extra)
    return env


# --------------------------------------------------------------------------------- [A]
def check_config_refuses_all_role():
    # Gate HIGH-1: the refusal is the HTTP startup's, not the config import's. The container's
    # alembic run and every one-shot CLI import app.config with the default role and N above 1.
    cp = subprocess.run(
        [PY, "-c", "from app import main; main._startup()"],
        cwd=_ROOT,
        env=_child_env(LOC_TMS_ROLE="all", LOC_TMS_WEB_PROCESSES="2"),
        capture_output=True,
        timeout=180,
    )
    err = cp.stderr.decode("utf-8", "replace")
    if cp.returncode == 0 or "LOC_TMS_WEB_PROCESSES" not in err:
        _fail(f"expected the all-role HTTP startup to refuse LOC_TMS_WEB_PROCESSES=2; got {cp}")
    env = {k: v for k, v in _child_env(LOC_TMS_WEB_PROCESSES="3").items() if k != "LOC_TMS_ROLE"}
    for argv, what in (
        ([PY, "-c", "from app import config; print(config.ROLE, config.WEB_PROCESSES)"], "config"),
        ([PY, "-m", "app.cli", "--help"], "the CLI"),
    ):
        ok = subprocess.run(argv, cwd=_ROOT, env=env, capture_output=True, timeout=180)
        if ok.returncode != 0:
            _fail(f"expected {what} to import with LOC_TMS_WEB_PROCESSES=3 and no role; got {ok}")
    ok = subprocess.run(
        [PY, "-c", "from app import config; print(config.WEB_PROCESSES, config.multi_web())"],
        cwd=_ROOT,
        env=_child_env(LOC_TMS_ROLE="worker", LOC_TMS_WEB_PROCESSES="3"),
        capture_output=True,
        timeout=120,
    )
    if ok.returncode != 0 or ok.stdout.decode().split()[-2:] != ["3", "True"]:
        _fail(f"expected a worker to accept LOC_TMS_WEB_PROCESSES=3; got {ok}")
    print(
        "  [A] LOC_TMS_WEB_PROCESSES>1 refused by the all-role HTTP startup; config, CLI and the "
        "worker role load with it OK"
    )


# --------------------------------------------------------------------------------- [B]
_BOOT_CHILD = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from app.services import leader
with leader.boot_lock():
    t0 = time.time()
    time.sleep(1.0)
    print("SPAN", t0, time.time(), flush=True)
"""


def check_boot_lock_serializes():
    env = _child_env(LOC_TMS_ROLE="web")
    kids = [
        subprocess.Popen(
            [PY, "-c", _BOOT_CHILD, _ROOT],
            cwd=_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(3)
    ]
    spans = []
    for k in kids:
        out, err = k.communicate(timeout=120)
        m = re.search(rb"SPAN ([0-9.]+) ([0-9.]+)", out)
        if not m:
            _fail(f"boot-lock child printed no span: {out!r} {err[-800:]!r}")
        spans.append((float(m.group(1)), float(m.group(2))))
    spans.sort()
    for (_a0, a1), (b0, _b1) in itertools.pairwise(spans):
        if b0 < a1 - 0.01:
            _fail(f"expected boot-lock holders one at a time; spans overlap: {spans}")
    print("  [B] three processes took the boot lock one at a time OK")


# --------------------------------------------------------------------------------- [C]
_PRESENCE_CHILD = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from app.services import jobs, leader
leader.hold_presence(jobs.instance_id())
print("INSTANCE", jobs.instance_id(), flush=True)
time.sleep(600)
"""


def _row(kind, payload):
    with Session(engine) as s:
        job = Job(
            kind=kind,
            payload=payload,
            status=JobStatus.RUNNING,
            started_at=datetime.utcnow(),
            created_by="verify_multi_web",
        )
        s.add(job)
        s.commit()
        s.refresh(job)
        return job.id


def _status(jid):
    with Session(engine) as s:
        return s.get(Job, jid).status


def check_owner_aware_cleanups():
    peer = subprocess.Popen(
        [PY, "-c", _PRESENCE_CHILD, _ROOT],
        cwd=_ROOT,
        env=_child_env(LOC_TMS_ROLE="web"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    PROCS.append(peer)
    line = peer.stdout.readline().decode("utf-8", "replace")
    m = re.match(r"INSTANCE (\S+)", line)
    if not m:
        _fail(f"presence child printed no instance id: {line!r}")
    live = m.group(1)
    if not leader.present(live):
        _fail(f"expected the live peer {live} to read as present")
    gone = f"{socket.gethostname()}:999999:{uuid.uuid4().hex[:8]}"
    if leader.present(gone):
        _fail(f"expected a never-started owner {gone} to read as gone")
    stream = {"document_id": 1, "locale": "fr-FR", "live_stream": True}
    rows = {
        "api_live": _row("api_task", {"route": "x", "owner": live}),
        "api_gone": _row("api_task", {"route": "x", "owner": gone}),
        "api_ownerless": _row("api_task", {"route": "x"}),
        "live_live": _row("translate_document", {**stream, "owner": live}),
        "live_gone": _row("translate_document", {**stream, "owner": gone}),
        "live_ownerless": _row("translate_document", dict(stream)),
    }
    background_calls.fail_interrupted()
    jobs.fail_orphaned_live_rows()
    want = {
        "api_live": JobStatus.RUNNING,
        "api_gone": JobStatus.FAILED,
        "api_ownerless": JobStatus.FAILED,
        "live_live": JobStatus.RUNNING,
        "live_gone": JobStatus.FAILED,
        "live_ownerless": JobStatus.FAILED,
    }
    got = {k: _status(v) for k, v in rows.items()}
    if got != want:
        _fail(f"expected only the live peer's rows to survive the boot cleanup; got {got}")
    _kill(peer)
    deadline = time.time() + 30
    while leader.present(live) and time.time() < deadline:
        time.sleep(0.2)
    background_calls.fail_interrupted()
    jobs.fail_orphaned_live_rows()
    if _status(rows["api_live"]) != JobStatus.FAILED or _status(rows["live_live"]) != (
        JobStatus.FAILED
    ):
        _fail("expected the peer's rows to be failed once the peer process is gone")
    print("  [C] boot cleanups fail gone/ownerless owners' rows, never a live peer's OK")


# --------------------------------------------------------------------------------- [D-F]
def _request(port, method, path, key, body=None, ctype=None, conn=None):
    own = conn is None
    conn = conn or http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"X-Loc-Token": key}
    if own:
        headers["Connection"] = "close"
    if ctype:
        headers["Content-Type"] = ctype
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    if own:
        conn.close()
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = raw
    return resp.status, data, hdrs


def _multipart(fields, fname, content):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
            "Content-Type: text/html\r\n\r\n"
        ).encode()
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _start_layout(port):
    with tempfile.NamedTemporaryFile("w+b", prefix="mw_web_", suffix=".log", delete=False) as f:
        web_log = f.name
    with tempfile.NamedTemporaryFile("w+b", prefix="mw_wrk_", suffix=".log", delete=False) as f:
        wrk_log = f.name
    common = {"LOC_TMS_WEB_PROCESSES": "2", "LOC_TMS_WORKER_PROCESSES": "1"}
    worker = subprocess.Popen(
        [PY, "-m", "app.worker"],
        cwd=_ROOT,
        env=_child_env(LOC_TMS_ROLE="worker", **common),
        stdout=open(wrk_log, "wb"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        **_GROUP,
    )
    PROCS.append(worker)
    web = subprocess.Popen(
        [
            PY,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "2",
            "--no-access-log",
        ],
        cwd=_ROOT,
        env=_child_env(LOC_TMS_ROLE="web", **common),
        stdout=open(web_log, "wb"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        **_GROUP,
    )
    PROCS.append(web)
    return web, web_log, worker, wrk_log


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def check_live_layout():
    user = security.create_user("mw@example.test", "MW", Role.ADMIN, FIXTURE_PASSWORD)
    key_a = apikeys.create_key(user.id, "mw-a", scopes="api docs")[1]
    key_b = apikeys.create_key(user.id, "mw-b", scopes="api docs")[1]
    port = _free_port()
    web, web_log, worker, wrk_log = _start_layout(port)
    deadline = time.time() + 180
    while time.time() < deadline:
        text = _read(web_log)
        if text.count("Application startup complete") >= 2:
            break
        if web.poll() is not None or worker.poll() is not None:
            _fail(
                f"a process exited at boot: web {_read(web_log)[-2000:]!r} worker "
                f"{_read(wrk_log)[-2000:]!r}"
            )
        time.sleep(0.3)
    else:
        _fail(f"expected two web processes to finish startup; log: {_read(web_log)[-3000:]!r}")
    pids = set(re.findall(r"Started server process \[(\d+)\]", _read(web_log)))
    if len(pids) < 2:
        _fail(f"expected two web server processes, saw {pids}; log {_read(web_log)[-2000:]!r}")
    # [D] a translate job end to end through the web processes and the worker.
    st, d, _h = _request(
        port,
        "POST",
        "/api/projects",
        key_a,
        json.dumps(
            {"name": "MW", "source_locale": "en-US", "targets": "fr-FR,de-DE", "provider": "mock"}
        ),
        "application/json",
    )
    if st >= 300:
        _fail(f"project create: {st} {d}")
    body, ctype = _multipart(
        {"project_id": d["id"]}, "page.html", b"<html><body><p>Hello there.</p></body></html>"
    )
    st, d, _h = _request(port, "POST", "/api/documents", key_a, body, ctype)
    if st >= 300:
        _fail(f"upload: {st} {d}")
    st, d, _h = _request(
        port,
        "POST",
        f"/api/documents/{d['document_id']}/translate",
        key_a,
        json.dumps({"targets": ["fr-FR", "de-DE"]}),
        "application/json",
    )
    if st >= 300:
        _fail(f"translate: {st} {d}")
    job_ids = [j["job_id"] for j in d["jobs"]]
    done = {}
    deadline = time.time() + 120
    while len(done) < len(job_ids) and time.time() < deadline:
        for jid in job_ids:
            if jid in done:
                continue
            st, d, _h = _request(port, "GET", f"/api/jobs/{jid}", key_a)
            if st == 200 and d.get("status") in ("completed", "failed"):
                done[jid] = d["status"]
        time.sleep(0.5)
    if done != dict.fromkeys(job_ids, "completed"):
        _fail(
            f"expected both translate jobs completed via the worker; got {done}; worker log "
            f"{_read(wrk_log)[-2000:]!r}"
        )
    from app.services import audit

    ok, checked, bad = audit.verify_chain()
    if not ok:
        _fail(
            f"expected the audit chain written by 2 web processes + a worker to verify; "
            f"bad row {bad} after {checked}"
        )
    print(
        "  [D] 2 web processes + 1 worker: translate queued, run and polled to completion, "
        f"audit chain of {checked} rows verifies OK"
    )
    # [E] the read budget is one budget across the web processes.
    settings.set_many({"api_read_rate_limit_per_minute": str(READ_LIMIT)}, actor="verify")
    time.sleep(settings.CHECK_INTERVAL_SECONDS + 1.5)
    codes = [_request(port, "GET", f"/api/jobs/{job_ids[0]}", key_b)[0] for _ in range(90)]
    allowed = codes.count(200)
    if allowed > READ_LIMIT or 429 not in codes:
        _fail(
            f"expected at most {READ_LIMIT} answers across both web processes; got "
            f"{allowed} (codes {sorted(set(codes))})"
        )
    print(f"  [E] read budget {READ_LIMIT}/min held across 2 web processes ({allowed} of 90) OK")
    settings.set_many({"api_read_rate_limit_per_minute": "0"}, actor="verify")
    time.sleep(settings.CHECK_INTERVAL_SECONDS + 1.5)
    # [F] some responses on a kept-alive connection close it (rebalancing).
    closes = 0
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    for _ in range(40):
        st, _d, hdrs = _request(port, "GET", f"/api/jobs/{job_ids[0]}", key_a, conn=conn)
        if hdrs.get("connection", "").lower() == "close":
            closes += 1
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.close()
    if closes < 1:
        _fail("expected some multi-web responses to carry Connection: close (rebalancing)")
    print(f"  [F] kept-alive connections rebalanced ({closes} closes in 40 requests) OK")
    for p in (web, worker):
        _kill(p)


def check_single_process_unchanged():
    code = (
        "from app import main\n"
        "names = [m.cls.__name__ for m in main.app.user_middleware]\n"
        "print('REBALANCE' if '_ConnectionRebalanceMiddleware' in names else 'PLAIN')\n"
    )
    cp = subprocess.run(
        [PY, "-c", code],
        cwd=_ROOT,
        env=_child_env(LOC_TMS_ROLE="all"),
        capture_output=True,
        timeout=180,
    )
    if cp.returncode != 0 or b"PLAIN" not in cp.stdout:
        _fail(f"expected no rebalancing layer in the single process; got {cp}")
    print("  [F] the single `all` process has no rebalancing layer OK")


def check_audit_hop_only_for_writes():
    """Gate round 3 HIGH: in the split roles the middleware audit append runs on a worker
    thread, but only for a request that can get a row. Reads and the liveness probes are
    decided on the event loop and never queue for a threadpool token (#99/#495)."""
    import asyncio

    from starlette.requests import Request

    from app import main as app_main
    from app.web import streaming as sc

    hops = []
    real = sc.run_audit

    async def counting(func, *a, **kw):
        hops.append(func.__name__)
        return False

    def req(method, path):
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "raw_path": path.encode(),
                "root_path": "",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 80),
                "scheme": "http",
                "app": app_main.app,
            }
        )

    sc.run_audit = counting
    try:
        for method, path in (("GET", "/health"), ("GET", "/ready"), ("GET", "/api/jobs/1")):
            for prerecord in (True, False):
                asyncio.run(app_main._audit_off_loop(req(method, path), 200, prerecord=prerecord))
        if hops:
            _fail(f"expected reads and probes to be decided on the event loop; hops: {hops}")
        asyncio.run(app_main._audit_off_loop(req("POST", "/api/projects"), 200))
        if hops != ["_audit_mutating_request"]:
            _fail(f"expected a mutating request's row to be written on a thread; hops: {hops}")
    finally:
        sc.run_audit = real
    print("  [G] audit thread hop only for mutating requests; probes and reads stay on the loop OK")


def main():
    init_db()
    settings.set_many(
        {
            "security_auth_required": "0",
            "multi_org_enabled": "0",
            "api_rate_limit_per_minute": "0",
            "api_read_rate_limit_per_minute": "0",
            "job_queue_backend": "db",
        },
        actor="verify_multi_web",
    )
    settings.invalidate_cache()
    try:
        check_config_refuses_all_role()
        check_boot_lock_serializes()
        check_owner_aware_cleanups()
        check_single_process_unchanged()
        check_audit_hop_only_for_writes()
        check_live_layout()
    finally:
        for p in PROCS:
            _kill(p)
    print("verify_multi_web: all checks OK")


if __name__ == "__main__":
    main()
