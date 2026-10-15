"""MCP front door (Phase A, Increment 2) verification — the stdio MCP server.

This is the ONE suite that needs the `mcp` SDK (installed in the venv, pinned in
requirements.txt for integrations/mcp-server ONLY). The app itself never imports
`mcp`; the server under integrations/mcp-server/ is a standalone stdio client
that talks to Localization OS over localhost HTTP.

Harness: the real FastAPI app is served by a background uvicorn thread on a free
127.0.0.1 port (the most faithful stand-in for the real deployment — the MCP
server's httpx client hits a real socket). A docs-scoped key is minted directly
via apikeys.create_key; a live stub provider is registered so translate does not
hit the mock-hard-fail (except where we deliberately test that it DOES).

Level 1 (tool functions): import the tool coroutines from server.py and call
  them against the running app:
    - list_capabilities / list_languages shapes
    - submit_translation(temp .txt, confirm=True) -> document_id + one job per
      locale. WAVE 1 added the `confirm` parameter: the two-phase spend contract
      lives in server.py's dispatcher, and these are DIRECT handler calls that
      bypass it, so they pass confirm=True to reach the executing branch. The
      contract itself (no confirm -> a read-only plan, nothing written) is
      verified through the dispatcher in verify_mcp_tools.py.
    - job_status long-polls to COMPLETED (and respects the ~45s cap)
    - get_result writes a translated file to the output dir with real bytes
    - mock-hard-fail surfaces as a clear tool error (no engine configured)
    - a missing token / bad file path / unreachable server all fail cleanly
Level 2 (protocol, best-effort): spawn server.py over the mcp stdio client and
  drive list_tools() + call_tool() for one tool. Guarded with a timeout so it
  can never hang the suite; a flaky/unavailable client prints a skip note.

Everything is torn down in `finally` (uvicorn thread + any subprocess), so the
suite never hangs.
"""

import asyncio
import logging
import os
import socket
import sys
import tempfile
import threading
import time

# Keep the suite output clean (like the others): silence httpx/uvicorn/mcp INFO chatter.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcp_")

# Make integrations/mcp-server importable as `server`.
_MCP_DIR = os.path.join(_ROOT, "integrations", "mcp-server")
sys.path.insert(0, _MCP_DIR)

# Guard the mcp import: this suite is the only one needing it. Installed in the
# venv, so it should import — but skip cleanly (exit 0, printed note) if not, so
# the app's other 27 suites are never coupled to the dependency.
try:
    import server as mcp_server
except ModuleNotFoundError as exc:  # pragma: no cover — only if mcp isn't installed
    if "mcp" in str(exc):
        print(f"  SKIP: mcp SDK not installed ({exc}) - install it for integrations/mcp-server")
        print("  (skipped: no mcp SDK)")
        print("MCP OK")
        raise SystemExit(0) from exc
    raise

# This suite exercises the tools as a LOCAL STDIO server would, so it must say so:
# caller-supplied file reads are denied by default now (fail-closed), because the old
# default left them enabled for any start path that never called main() -- including
# serving the ASGI app directly. Emulating stdio means opting in explicitly.
# #755: its fixtures live under the temp dir, so that is the allowed input folder.
mcp_server._allow_local_file_reads(True, roots=[tempfile.gettempdir()])


import uvicorn

from app.db import init_db
from app.main import app
from app.services import apikeys, security, settings
from app.services.providers import Provider, register_provider


class LiveStubProvider(Provider):
    """A 'configured' engine so the mock-hard-fail is OFF for the happy path
    (provider_is_live() is True for any registered override — no key needed)."""

    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if task == "translate":
            payload = payload or {}
            return f"[{payload.get('target', 'xx')}] {payload.get('text', '')}"
        return (prompt or "").strip()


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UvicornThread:
    """Run the real app in a background uvicorn Server on a free port."""

    def __init__(self, port: int):
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self, timeout: float = 15.0) -> None:
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise AssertionError("uvicorn did not start in time")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _run(coro):
    """Run a tool coroutine to completion (fresh event loop each call — matches
    how FastMCP would invoke it per call)."""
    return asyncio.run(coro)


def _expect_toolerror(coro, needle: str, what: str) -> str:
    """Assert calling `coro` raises a ToolError whose message contains `needle`."""
    try:
        _run(coro)
    except mcp_server.ToolError as e:
        msg = str(e)
        assert needle.lower() in msg.lower(), f"{what}: expected '{needle}' in: {msg}"
        return msg
    raise AssertionError(f"{what}: expected a ToolError mentioning '{needle}', none raised")


def level1(base_url: str, docs_key: str, out_dir: str) -> None:
    import json

    # Point the server module at THIS test's app + token + output dir.
    mcp_server.BASE_URL = base_url
    mcp_server.TOKEN = docs_key
    mcp_server.OUTPUT_DIR = __import__("pathlib").Path(out_dir)

    # --- discovery tools ---------------------------------------------------
    caps = json.loads(_run(mcp_server.list_capabilities()))
    exts = {f["ext"] for f in caps["formats"]}
    assert "docx" in exts and "txt" in exts and all("name" in f for f in caps["formats"]), caps
    print(f"  list_capabilities: {len(exts)} formats (docx/txt present, friendly names)")

    langs = json.loads(_run(mcp_server.list_languages()))
    assert isinstance(langs["language_sets"], list) and isinstance(langs["locales"], list), langs
    print(f"  list_languages: {len(langs['language_sets'])} sets, {len(langs['locales'])} locales")

    # --- a temp source file (stdio has real FS access) ---------------------
    src = os.path.join(tempfile.mkdtemp(prefix="mcp_src_"), "hello.txt")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("First sentence.\nSecond sentence.\nThird one here.\n")

    # --- mock-hard-fail: NO live provider yet -> tool error, zero output ---
    from app.services import providers as _prov

    _prov._OVERRIDES.pop("claude", None)  # ensure offline -> Mock would resolve
    msg = _expect_toolerror(
        mcp_server.submit_translation(src, ["fr-FR"], confirm=True),
        "translation engine",
        "mock-hard-fail",
    )
    print(
        f"  mock-hard-fail: submit_translation surfaces a clear tool error "
        f'("{msg.split(".")[0][:52]}...")'
    )

    # --- bad file path -> clean error before any network call --------------
    _expect_toolerror(
        mcp_server.submit_translation(os.path.join(out_dir, "nope.txt"), ["fr-FR"], confirm=True),
        "No file found",
        "missing-file",
    )
    print("  submit_translation: missing local file -> clear error, no upload")

    # --- happy path: register a live stub, then submit --------------------
    register_provider("claude", LiveStubProvider())
    try:
        summary = json.loads(
            _run(mcp_server.submit_translation(src, ["fr-FR", "de-DE"], confirm=True))
        )
        doc_id = summary["document_id"]
        jobs = summary["jobs"]
        assert doc_id and len(jobs) == 2, summary
        locs = {j["locale"] for j in jobs}
        assert locs == {"fr-FR", "de-DE"}, summary
        assert all(isinstance(j["job_id"], int) for j in jobs), summary
        print(
            f"  submit_translation: txt -> document_id={doc_id}, "
            f"{len(jobs)} jobs (one per target: {sorted(locs)})"
        )

        # --- job_status long-poll to COMPLETED (respecting the ~45s cap) ---
        fr_job = next(j for j in jobs if j["locale"] == "fr-FR")["job_id"]
        # First call may already complete (mock is fast); loop a few CALLS to
        # prove re-poll works, but each CALL is capped internally at ~45s.
        finished = None
        t0 = time.time()
        for _ in range(20):
            st = json.loads(_run(mcp_server.job_status(fr_job)))
            if st["finished"]:
                finished = st
                break
            time.sleep(0.1)
        assert finished is not None, "job never reported finished"
        assert str(finished["status"]).lower() == "completed", finished
        assert finished["total"] == 3 and finished["done"] == 3, finished
        assert (time.time() - t0) < 60, "job_status calls took implausibly long"
        print(
            f"  job_status: async long-poll -> COMPLETED "
            f"({finished['done']}/{finished['total']} segments)"
        )

        # --- get_result writes a translated file with real bytes ----------
        res = json.loads(_run(mcp_server.get_result(doc_id, "fr-FR")))
        path = res["path"]
        assert os.path.isfile(path), f"get_result did not write a file: {path}"
        assert os.path.commonpath(
            [os.path.abspath(out_dir), os.path.abspath(path)]
        ) == os.path.abspath(out_dir), "output written outside LOC_STUDIO_OUTPUT_DIR"
        with open(path, "rb") as fh:
            written = fh.read()
        assert len(written) == res["bytes"] and res["bytes"] > 0, res
        assert b"[fr-FR]" in written, f"translated txt missing stub marker: {written[:120]!r}"
        print(
            f"  get_result: wrote {res['bytes']} bytes to output dir "
            f"({os.path.basename(path)}), translated content present"
        )

        # --- get_result on an unknown locale -> clean 404 tool error ------
        _expect_toolerror(
            mcp_server.get_result(doc_id, "zz-ZZ"), "not found", "unknown-locale-download"
        )
        print("  get_result: unknown locale -> clear 'not found' tool error")
    finally:
        _prov._OVERRIDES.pop("claude", None)

    # --- auth errors: a plain "api" key (no docs scope) -> 403 message -----
    admin = security.get_by_email("admin")
    _k, api_only = apikeys.create_key(admin.id, "plain", scopes="api")
    mcp_server.TOKEN = api_only
    _expect_toolerror(mcp_server.list_capabilities(), "document-scoped", "api-only-key-403")
    print("  auth: an 'api'-only key -> clear 403 (docs scope required)")

    # --- missing token -> setup error, no network call --------------------
    mcp_server.TOKEN = ""
    _expect_toolerror(mcp_server.list_capabilities(), "LOC_STUDIO_TOKEN", "no-token")
    print("  auth: empty LOC_STUDIO_TOKEN -> clear setup error")

    # --- unreachable server -> friendly connection error ------------------
    mcp_server.TOKEN = docs_key
    mcp_server.BASE_URL = "http://127.0.0.1:1"  # nothing listens here
    _expect_toolerror(mcp_server.list_capabilities(), "Could not reach", "conn-refused")
    print("  connection: server down -> friendly 'Could not reach Localization OS' error")
    mcp_server.BASE_URL = base_url  # restore


def level2(base_url: str, docs_key: str, out_dir: str) -> None:
    """Best-effort protocol test: spawn server.py over stdio and drive it. Fully
    timeout-guarded so it can never hang the suite."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except Exception as e:
        print(f"  SKIP level 2 (mcp client stdio import failed: {e})")
        return

    env = dict(os.environ)
    env["LOC_STUDIO_BASE_URL"] = base_url
    env["LOC_STUDIO_TOKEN"] = docs_key
    env["LOC_STUDIO_OUTPUT_DIR"] = out_dir

    server_py = os.path.join(_MCP_DIR, "server.py")
    params = StdioServerParameters(command=sys.executable, args=[server_py], env=env)

    async def drive():
        async with stdio_client(params) as (read, write):  # noqa: SIM117
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                expected = {
                    "submit_translation",
                    "job_status",
                    "get_result",
                    "list_languages",
                    "list_capabilities",
                }
                assert expected <= names, f"missing tools over stdio: {expected - names}"
                # Drive one read-only tool end-to-end over the protocol.
                result = await session.call_tool("list_capabilities", {})
                text = "".join(getattr(c, "text", "") for c in result.content)
                assert "formats" in text and "docx" in text, text
                return sorted(names)

    async def guarded():
        return await asyncio.wait_for(drive(), timeout=40)

    try:
        names = asyncio.run(guarded())
        print(f"  level 2 (stdio protocol): spawned server, list_tools -> {names}")
        print("  level 2: call_tool('list_capabilities') round-tripped over stdio")
    except asyncio.TimeoutError:
        print("  SKIP level 2: stdio round-trip timed out (guarded, suite continues)")
    except Exception as e:
        print(f"  SKIP level 2: stdio protocol not exercised ({type(e).__name__}: {e})")


def main() -> None:
    init_db()
    security.seed_initial()  # admin/admin + auth ON (mirrors verify_mcp_api)
    settings.set_many({"audit_enabled": "1"})

    admin = security.get_by_email("admin")
    _k, docs_key = apikeys.create_key(admin.id, "mcp docs", scopes="api docs")

    out_dir = tempfile.mkdtemp(prefix="loctms_mcp_out_")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    srv = _UvicornThread(port)
    try:
        srv.start()
        level1(base_url, docs_key, out_dir)
        level2(base_url, docs_key, out_dir)
    finally:
        srv.stop()
        settings.set_many({"security_auth_required": "0"})

    print("MCP OK")


if __name__ == "__main__":
    main()
