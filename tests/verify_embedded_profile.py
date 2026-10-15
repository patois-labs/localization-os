"""Embedded / headless profile verification (offline) — composable-core phase 0, 0a.

Regression guard for `LOC_TMS_EMBEDDED=1`: the app recomposes as an agent-only service
(no Jinja web UI, no /static, no scheduler/retention daemons) while
keeping /health + /ready, the FULL token-authed /api surface, and the external-cron
POST /api/admin/tick. The login wall is ON in embedded so /api RBAC is real.

Boot-simulation pattern (copied from verify_docker_auth.py): this process boots the app
with LOC_TMS_EMBEDDED=1 and asserts the embedded assembly; it then spawns a SIBLING
child process (this same file with --server-child) that boots a NORMAL, non-embedded
server and asserts the assembly is UNCHANGED (static mounted, wall + both
daemons started, strictly MORE routes than embedded). Running the two boots in separate
processes is required because config reads the mode env ONCE at import time.

THE LOAD-BEARING GUARD (HIGH 3 / B1): the embedded boot must reach EVERY /api route the
default boot serves. The child writes its (method, path) set for every /api* route to a
file; the parent asserts the embedded set is EQUAL — so a mixed web+api module that
lands its /api routes only in a WEB router (dropped by embedded) fails this suite by
construction, not by a human noticing.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys, never touches the real DB.
ASCII-only prints. Final line: EMBEDDED PROFILE OK.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

# Three modes, decided from argv BEFORE any app import (config freezes the mode +
# the DB backend at import). The pg-config child imports ONLY app.config (no engine,
# no connection), so it needs a Postgres DATABASE_URL but no live Postgres.
_SERVER_CHILD = "--server-child" in sys.argv
_PG_CHILD = "--pg-config-child" in sys.argv
# A syntactically-valid Postgres DSN that is NEVER connected to (config just stores
# the string; app.db / app.main are not imported in the pg-config child).
_FAKE_PG_URL = "postgresql+psycopg://loc:loc@127.0.0.1:5432/loc_embedded_probe"

if _PG_CHILD:
    init_env("loctms_embpg_")
    os.environ.pop("LOC_TMS_SERVER", None)
    os.environ["LOC_TMS_EMBEDDED"] = "1"
    os.environ["DATABASE_URL"] = _FAKE_PG_URL
elif _SERVER_CHILD:
    init_env("loctms_embserver_")
    os.environ.pop("LOC_TMS_EMBEDDED", None)
    os.environ["LOC_TMS_SERVER"] = "1"  # a NORMAL, non-embedded server boot
else:
    init_env("loctms_embedded_")
    os.environ.pop("LOC_TMS_SERVER", None)
    os.environ["LOC_TMS_EMBEDDED"] = "1"

if _PG_CHILD:
    # Config-only: prove the DB backend selection WITHOUT a live Postgres. Importing
    # app.config never opens a connection (the engine is built in app.db, which we do
    # NOT import). This is the executable form of the section-5 SUPPORTED claim:
    # embedded + a Postgres DATABASE_URL selects Postgres and stays embedded.
    from app import config as _pg_config

    def pg_config_child_main() -> None:
        assert _pg_config.IS_EMBEDDED is True, "pg-config child must be embedded"
        assert _pg_config.IS_SERVER is True, "embedded implies server posture"
        assert _pg_config.IS_TEST is False, "embedded + DATA_DIR is not a test boot"
        assert _pg_config.IS_SQLITE is False, (
            "a postgres DATABASE_URL must DESELECT SQLite (IS_SQLITE stayed True)"
        )
        assert _pg_config.DB_URL == _FAKE_PG_URL, (
            f"DATABASE_URL must win over the SQLite default: {_pg_config.DB_URL!r}"
        )
        print(
            "  pg-config-child: embedded + DATABASE_URL(postgres) -> IS_SQLITE False, "
            "DB_URL honored, still embedded (no connection opened)"
        )
        print("PG CONFIG CHILD OK")
else:
    from fastapi.testclient import TestClient
    from starlette.routing import Mount

    from app import config
    from app.main import app
    from app.services import retention, scheduler, security


def _http_routes() -> set:
    """Every (method, path) the app serves (a Mount has no `methods`)."""
    from app.web._routes import walk_routes

    out = set()
    for r in walk_routes(app):
        for m in getattr(r, "methods", None) or ():
            out.add((m, r.path))
    return out


def _api_routes() -> set:
    return {(m, p) for (m, p) in _http_routes() if p.startswith("/api")}


def _mount_paths() -> set:
    from app.web._routes import walk_routes

    return {r.path for r in walk_routes(app) if isinstance(r, Mount)}


def _mk_user_key(role: str, scopes: str) -> str:
    """Mint a user of `role` + a key with `scopes` (in-process, so `admin` scope is
    allowed). Returns the raw lsu_ key."""
    import secrets as _secrets

    from app.services import apikeys

    email = f"emb-{_secrets.token_hex(4)}@x.com"
    user = security.create_user(email, email, role, "emb-" + _secrets.token_hex(6))
    _row, raw = apikeys.create_key(user.id, "emb", scopes=scopes)
    return raw


def _run_cli(argv) -> tuple:
    """Run the in-process CLI, folding stderr in; SystemExit -> nonzero rc."""
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = cli_main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# The NORMAL (non-embedded) server boot — a sibling child process. Proves the
# assembly is unchanged, by CONTRAST with the embedded skips, and hands the parent
# its /api route set for the completeness guard.
# ---------------------------------------------------------------------------
def server_child_main() -> None:
    from app.models import Role

    assert config.IS_EMBEDDED is False, "server-child must NOT be embedded"
    assert config.IS_SERVER is True, "server-child is a real server boot"
    assert config.IS_TEST is False, "a declared server boot is not a test boot"

    # Patch the two daemon starters to RECORD (never spawn a real thread) so we can
    # prove a normal server boot DOES start them where embedded skips them.
    started = {"scheduler": 0, "retention": 0}
    orig_sched, orig_ret = scheduler.start, retention.start
    scheduler.start = lambda: started.__setitem__("scheduler", started["scheduler"] + 1)
    retention.start = lambda: started.__setitem__("retention", started["retention"] + 1)
    try:
        with TestClient(app) as c:
            mounts = _mount_paths()
            assert "/static" in mounts, f"normal boot must mount /static ({mounts!r})"
            # 2026-09-20: /back-office was removed for EVERY profile, not just embedded.
            assert "/back-office" not in mounts, f"the console is gone ({mounts!r})"
            assert security.auth_required() is True, "normal server boot walls the app"
            assert started["scheduler"] == 1, "normal server boot must start the scheduler"
            assert started["retention"] == 1, "normal server boot must start retention"
            for p in ("/", "/projects", "/help/llm"):
                assert c.get(p, follow_redirects=False).status_code != 404, (
                    f"normal boot: {p} must be routed (got 404)"
                )
            assert c.get("/api/projects").status_code == 401, "api needs a token, not 404"
            assert c.get("/api/admin/tick").status_code == 405, (
                "GET /api/admin/tick should be 405 (POST-only), proving the route exists"
            )
            assert c.get("/health").status_code == 200
            assert c.get("/ready").status_code == 200
            emb = int(os.environ.get("LOC_TMS_TEST_EMBEDDED_ROUTES", "0"))
            assert emb > 0 and len(_http_routes()) > emb, (
                "normal boot must register MORE HTTP routes than embedded (%d vs %d)"  # noqa: UP031
                % (len(_http_routes()), emb)
            )
    finally:
        scheduler.start, retention.start = orig_sched, orig_ret

    # Hand the parent the DEFAULT boot's /api route set for the completeness guard.
    out_path = os.environ.get("LOC_TMS_TEST_APIROUTES_OUT")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(sorted(list(_api_routes())), fh)  # noqa: C414
    assert Role  # silence unused (imported for symmetry with the embedded arm)
    print(
        "  server-child: static mounted, no back-office, wall+scheduler+retention started, "  # noqa: UP031
        "web routes routed, %d routes > embedded, %d /api routes recorded OK"
        % (len(_http_routes()), len(_api_routes()))
    )
    print("SERVER CHILD OK")


# ---------------------------------------------------------------------------
# The EMBEDDED (headless) boot — this process.
# ---------------------------------------------------------------------------
def embedded_main() -> None:
    from app.models import Role

    # Config: embedded implies server posture, never a test boot.
    assert config.IS_EMBEDDED is True, "LOC_TMS_EMBEDDED=1 must set IS_EMBEDDED"
    assert config.IS_SERVER is True, "embedded implies server posture"
    assert config.IS_TEST is False, "an embedded boot (with DATA_DIR) must NOT be a test boot"
    print("  config: IS_EMBEDDED, IS_SERVER, not IS_TEST")

    # Assembly: /static is NOT mounted. (/back-office is not mounted anywhere any more,
    # so it is no longer an embedded-specific distinction; the assertion below is kept
    # as a floor rather than as a difference between the two profiles.)
    mounts = _mount_paths()
    assert "/static" not in mounts, f"embedded must NOT mount /static ({mounts!r})"
    assert "/back-office" not in mounts, f"embedded must NOT mount /back-office ({mounts!r})"
    embedded_routes = len(_http_routes())
    embedded_api = _api_routes()

    with TestClient(app) as c:
        # The daemons are skipped even though IS_SERVER is true...
        assert scheduler._started is False, "embedded must NOT start the scheduler daemon"
        assert retention._started is False, "embedded must NOT start the retention daemon"
        # ...but the LOGIN WALL is ON: security_auth_required is the master switch for
        # /api RBAC, so embedded turns it on (B2). This is the inverse of a test boot.
        assert security.auth_required() is True, (
            "embedded must enable the login wall so /api RBAC is enforced"
        )
        print("  startup: scheduler + retention NOT started, but the login wall IS enabled")

        # The Jinja web surface is not served: never a 200 (the wall bounces it, or the
        # route is absent). /static/* + /back-office are unmounted -> 404.
        for p in ("/", "/projects", "/help/llm", "/setup-ai", "/settings"):
            code = c.get(p, follow_redirects=False).status_code
            assert code != 200 and code in (303, 307, 404), (
                "embedded: web route %s must not serve content (got %d)" % (p, code)  # noqa: UP031
            )
        assert c.get("/static/app.css").status_code == 404, "/static/* must 404 when unmounted"
        assert c.get("/back-office").status_code == 404, "/back-office must 404 when unmounted"
        print(
            "  web surface gone: /, /projects, /help/llm, /setup-ai, /settings never 200; "
            "/static/*, /back-office 404"
        )

        # Health/readiness stay live (the container liveness surface).
        assert c.get("/health").status_code == 200, "/health must stay mounted"
        assert c.get("/ready").status_code == 200, "/ready must stay mounted"
        print("  liveness surface live: /health + /ready 200")

        admin_tok = _mk_user_key(Role.ADMIN, "docs admin")
        viewer_tok = _mk_user_key(Role.VIEWER, "docs")
        translator_tok = _mk_user_key(Role.TRANSLATOR, "docs")  # non-admin scope for tick

        # The /api surface works with a token (and the split-out keys/images /api routes
        # are proven present by the completeness guard at the end).
        assert c.get("/api/projects").status_code == 401, "no token -> 401"
        assert c.get("/api/projects", headers={"X-Loc-Token": admin_tok}).status_code == 200, (
            "a valid docs-scoped token must reach /api/projects"
        )

        # RBAC IS REAL in embedded (the whole point of the wall): a VIEWER-role docs key
        # passes _gate (docs scope) but is 403'd by the write-role gate on a write route.
        # With the wall off, _role_block no-ops and this would NOT be 403 (the B2 hole).
        r = c.post("/api/projects", headers={"X-Loc-Token": viewer_tok}, json={"name": "x"})
        assert r.status_code == 403, (
            "embedded RBAC: a viewer token must be 403 on a write route (got %d) -- the "  # noqa: UP031
            "login wall must be enforcing roles" % r.status_code
        )
        print("  /api RBAC enforced: viewer token 401->none, 200 admin read, 403 viewer write")

        # POST /api/admin/tick: admin-gated, and returns the COUNT only (no job ids).
        assert c.post("/api/admin/tick").status_code == 401, "tick: no token -> 401"
        assert (
            c.post("/api/admin/tick", headers={"X-Loc-Token": translator_tok}).status_code == 403
        ), "tick: a non-admin-scope token -> 403"
        r = c.post("/api/admin/tick", headers={"X-Loc-Token": admin_tok})
        assert r.status_code == 200, "tick: admin token -> 200 (got %d)" % r.status_code  # noqa: UP031
        body = r.json()
        assert body.get("ok") is True and body.get("ran") is True, body
        assert body.get("jobs_enqueued") == 0, f"a fresh install enqueues nothing: {body!r}"
        assert "fired" not in body, (
            "the tick response must NOT echo job ids (install-global -> cross-org leak): {!r}".format(  # noqa: E501, UP032
                body
            )
        )
        print(
            "  POST /api/admin/tick: 401 no-token, 403 non-admin, 200 admin "
            "(jobs_enqueued only, no 'fired')"
        )

        # The 409-busy branch: hold the tick lock, prove a concurrent tick is refused.
        got = scheduler._tick_lock.acquire(blocking=False)
        assert got, "expected the tick lock to be free"
        try:
            r = c.post("/api/admin/tick", headers={"X-Loc-Token": admin_tok})
            assert r.status_code == 409, "tick while locked -> 409 (got %d)" % r.status_code  # noqa: UP031
            b = r.json()
            assert b.get("busy") is True and b.get("ran") is False, b
        finally:
            scheduler._tick_lock.release()
        print("  POST /api/admin/tick while a tick holds the lock -> 409 busy")

        # The CLI `admin tick` verb, LOCAL arm (in-process): runs tick_now, count only.
        rc, out = _run_cli(["--json", "admin", "tick"])
        assert rc == 0, "CLI `admin tick` local arm rc=%d: %s" % (rc, out[:200])  # noqa: UP031
        assert "jobs_enqueued" in out and "fired" not in out, (
            f"CLI `admin tick` --json must report jobs_enqueued and never 'fired': {out[:200]}"
        )
        print("  CLI `admin tick` (local arm) runs and reports jobs_enqueued")

        # The CLI `admin tick` REMOTE arm: parses jobs_enqueued from the /api body, never
        # touches 'fired'. Stub the HTTP client so no server is needed.
        import types

        from app.cli import _remote_impl
        from app.cli._common import Emitter

        class _StubClient:
            def post(self, path, json=None):
                assert path == "/api/admin/tick", path
                return {"ok": True, "ran": True, "jobs_enqueued": 3}

        orig_client = _remote_impl._client
        set_pieces("app.cli._remote_impl", "_client", lambda args: _StubClient())
        try:
            rc = _remote_impl.admin_tick(types.SimpleNamespace(), Emitter(as_json=True))
            assert rc == 0, f"remote admin_tick returned {rc!r}"
        finally:
            set_pieces("app.cli._remote_impl", "_client", orig_client)
        print("  CLI `admin tick` (remote arm) parses jobs_enqueued from the /api body")

    # Prove the NON-embedded assembly is unchanged AND get its /api route set, via a
    # sibling server child.
    fd, api_out = tempfile.mkstemp(prefix="loctms_apiroutes_", suffix=".json")
    os.close(fd)
    child_env = dict(os.environ)
    child_env.pop("LOC_TMS_EMBEDDED", None)
    child_env.pop("LOC_TMS_DATA_DIR", None)  # the child's init_env mints its own
    child_env["LOC_TMS_TEST_EMBEDDED_ROUTES"] = str(embedded_routes)
    child_env["LOC_TMS_TEST_APIROUTES_OUT"] = api_out
    child = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--server-child"],
        env=child_env,
        capture_output=True,
        text=True,
    )
    if child.returncode != 0:
        sys.stdout.write(child.stdout)
        sys.stderr.write(child.stderr)
        raise AssertionError("the non-embedded server-child boot FAILED (see output above)")
    for line in child.stdout.splitlines():
        if line.strip():
            print("  [child] " + line.strip())

    # THE COMPLETENESS GUARD (HIGH 3 / B1): embedded must serve EVERY /api route the
    # default boot does. A mixed web+api module whose /api routes land in a WEB router
    # (dropped by embedded) fails HERE, structurally.
    with open(api_out, encoding="utf-8") as fh:
        default_api = {tuple(x) for x in json.load(fh)}
    os.remove(api_out)
    assert default_api, "the child recorded no /api routes -- the guard would be vacuous"
    missing = sorted(default_api - embedded_api)
    assert not missing, (
        "embedded DROPS %d /api route(s) the default boot serves (a mixed web+api module "  # noqa: UP031
        "whose /api routes must move to an api sub-router):\n  %s"
        % (len(missing), "\n  ".join(f"{m} {p}" for m, p in missing))
    )
    # And embedded adds no phantom /api route the default lacks.
    extra = sorted(embedded_api - default_api)
    assert not extra, f"embedded serves /api routes the default boot does not: {extra!r}"
    print(
        "  /api completeness: embedded serves all %d default /api routes, none extra OK"  # noqa: UP031
        % len(default_api)
    )

    # Postgres backend selection under embedded (composable-core phase 0, Train 0c).
    # A config-only child proves embedded + a Postgres DATABASE_URL selects Postgres
    # and stays embedded, WITHOUT a live Postgres (the live schema is CI's pg_smoke
    # job). Spawned as a sibling because config freezes the backend at import.
    pg_env = dict(os.environ)
    pg_env.pop("LOC_TMS_EMBEDDED", None)  # the child sets its own mode + DATABASE_URL
    pg_env.pop("LOC_TMS_DATA_DIR", None)  # the child's init_env mints its own
    pg_env.pop("DATABASE_URL", None)
    pg_child = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--pg-config-child"],
        env=pg_env,
        capture_output=True,
        text=True,
    )
    if pg_child.returncode != 0:
        sys.stdout.write(pg_child.stdout)
        sys.stderr.write(pg_child.stderr)
        raise AssertionError("the embedded pg-config child boot FAILED (see output above)")
    for line in pg_child.stdout.splitlines():
        if line.strip():
            print("  [pg-child] " + line.strip())
    assert "PG CONFIG CHILD OK" in pg_child.stdout, "pg-config child did not confirm OK"

    print("EMBEDDED PROFILE OK")


def main() -> None:
    if _PG_CHILD:
        pg_config_child_main()
    elif _SERVER_CHILD:
        server_child_main()
    else:
        embedded_main()


if __name__ == "__main__":
    main()
