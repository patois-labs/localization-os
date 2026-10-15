"""#518 - the MCP server never writes exports to its own disk for a remote caller, and
`manage_install action=jev_try` is two-phase like every spend call.

  1. Without local file access (any start path except a local stdio server) the export
     helper returns the content inline (UTF-8 text, or base64 for binary), writes nothing
     under OUTPUT_DIR and leaks no host path.
  2. An oversized remote export is refused with a pointer to the web UI / API.
  3. With local file access (stdio) the export is still written to OUTPUT_DIR as before.
  4. Every export tool hands its bytes to `_write_output` (so every one inherits the gate).
  5. jev_try without confirm=["jev_try"] returns a plan and never calls the API.

Offline; ASCII-only prints.
"""

import ast
import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from tests._bootstrap import init_env

init_env("loctms_mcpremote_")
sys.path.insert(0, str(ROOT / "integrations" / "mcp-server"))
import server as mcp_server

EXPORT_TOOLS = (
    "get_result",
    "export_keys",
    "list_assets",
    "manage_tm",
    "list_images",
)


def ok(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)


def test_remote_returns_content_and_writes_nothing():
    out_dir = Path(tempfile.mkdtemp(prefix="loctms_mcpout_"))
    with (
        patch.object(mcp_server, "OUTPUT_DIR", out_dir),
        patch.object(mcp_server, "_LOCAL_FILE_READS", False),
    ):
        text = mcp_server._write_output("memories.jsonl", '{"a": "café"}\n'.encode())
        blob = bytes(range(256))
        binary = mcp_server._write_output("../../evil/doc.docx", blob)
    ok("path" not in text and "path" not in binary, f"a remote reply leaked a host path: {text}")
    ok(text["content"] == '{"a": "café"}\n' and text["encoding"] == "utf-8", text)
    ok(
        text["filename"] == "memories.jsonl" and text["bytes"] == len(text["content"].encode()),
        text,
    )
    ok(binary["encoding"] == "base64" and "content" not in binary, binary)
    ok(base64.b64decode(binary["content_base64"]) == blob, "binary content must round-trip")
    ok(binary["filename"] == "doc.docx", f"the name must be the basename only: {binary}")
    ok(list(out_dir.iterdir()) == [], f"a remote export wrote to disk: {list(out_dir.iterdir())}")
    ok(str(out_dir) not in json.dumps([text, binary]), "the host path appeared in a reply")
    print("  [1] remote: content returned inline, nothing written, no host path OK")


def test_remote_oversize_refused():
    big = b"x" * (mcp_server._INLINE_OUTPUT_MAX_BYTES + 1)
    with patch.object(mcp_server, "_LOCAL_FILE_READS", False):
        try:
            mcp_server._write_output("big.txt", big)
        except mcp_server.ToolError as exc:
            ok("web UI" in str(exc), f"the refusal must say where to download: {exc}")
        else:
            ok(False, "an oversized remote export must be refused")
    print("  [2] remote: an oversized export is refused with a pointer OK")


def test_stdio_still_writes():
    out_dir = Path(tempfile.mkdtemp(prefix="loctms_mcpout_"))
    with (
        patch.object(mcp_server, "OUTPUT_DIR", out_dir),
        patch.object(mcp_server, "_LOCAL_FILE_READS", True),
    ):
        res = mcp_server._write_output("memories.jsonl", b"line\n")
    ok(Path(res["path"]).read_bytes() == b"line\n" and res["bytes"] == 5, res)
    ok(Path(res["path"]).parent == out_dir.resolve(), res)
    print("  [3] stdio: the export is still written under OUTPUT_DIR OK")


def test_every_export_tool_uses_the_gate():
    tree = ast.parse((ROOT / "integrations" / "mcp-server" / "server.py").read_text("utf-8"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    for name in EXPORT_TOOLS:
        calls = {
            c.func.id
            for c in ast.walk(funcs[name])
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        ok("_write_output" in calls, f"{name} no longer goes through _write_output")
        ok(
            not any(
                isinstance(c, ast.Attribute) and c.attr in ("write_bytes", "write_text")
                for c in ast.walk(funcs[name])
            ),
            f"{name} writes a file itself, bypassing the remote gate",
        )
    print("  [4] all %d export tools go through the gated writer OK" % len(EXPORT_TOOLS))  # noqa: UP031


def test_jev_try_is_two_phase():
    sent = []

    class Probe:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_post(client, path, body):
        sent.append((path, body))
        return {"ok": True, "verdict": "pass"}

    args = {
        "action": "jev_try",
        "engine_id": 7,
        "source": "Pay ten euros.",
        "target": "Zahlen Sie zehn Euro.",
        "source_locale": "en",
        "target_locale": "de",
    }
    with (
        patch.object(mcp_server, "_client", lambda: Probe()),
        patch.object(mcp_server, "_post_json", fake_post),
    ):
        plan = json.loads(asyncio.run(mcp_server.manage_install(**args)))
        ok(plan.get("plan") is True and sent == [], f"unconfirmed jev_try must only plan: {plan}")
        for bad in (True, "jev_try", ["set_config"], [], ["JEV_TRY"]):
            plan2 = json.loads(asyncio.run(mcp_server.manage_install(confirm=bad, **args)))
            ok(plan2.get("plan") is True and sent == [], f"confirm={bad!r} must not spend")
        done = json.loads(asyncio.run(mcp_server.manage_install(confirm=["jev_try"], **args)))
        ok(len(sent) == 1 and sent[0][0].endswith("/engines/7/jev-try"), sent)
        ok(done.get("verdict") == "pass", done)
    print("  [5] jev_try: plan without confirm, spends only with confirm=[jev_try] OK")


def main():
    test_remote_returns_content_and_writes_nothing()
    test_remote_oversize_refused()
    test_stdio_still_writes()
    test_every_export_tool_uses_the_gate()
    test_jev_try_is_two_phase()
    print("MCP REMOTE EXPORTS OK")


if __name__ == "__main__":
    main()
