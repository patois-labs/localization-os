"""MCP server: local file reads are confined (#755) and confirm is bound to a plan (#757).

Offline: no Localization OS app is started; the tools' handlers and planners are stubbed
where a test needs them, and every refusal is proved to happen BEFORE any /api call.

#755 (stdio file_path reads, integrations/mcp-server/loc_file_guard.py):
  F1  outside the allowed input folders -> refused (message names the folders + env var)
  F2  a dotfile or a file under a dot-directory inside an allowed folder -> refused
  F3  a symlink inside an allowed folder pointing outside it -> refused (when the OS lets
      the test create one)
  F4  an extension the receiving route would not accept, or none -> refused
  F5  over LOC_STUDIO_MAX_INPUT_BYTES -> refused before reading; images capped at 8 MiB
  F6  LOC_STUDIO_INPUT_DIRS replaces the defaults; the output dir is always allowed;
      relative and missing roots are ignored
  F7  submit_translation with a refused path raises before ANY /api call; its plan
      reports the refusal as a warning and never reads the file
  F8  a permitted file is read and returned byte-identical

#757 (plan_id, integrations/mcp-server/loc_plan_binding.py + server._dispatch):
  P1  every spend tool's schema has `plan_id` next to `confirm`; no other tool has it
  P2  a plan returns a plan_id; confirm without it -> spend_refused, handler not called
  P3  changed arguments (3 locales planned, 9 confirmed), another tool's plan, a reused
      plan, an expired plan, another caller's plan -> spend_refused, handler not called
  P4  the matching plan_id runs the handler exactly once, without plan_id in its kwargs
  P5  MRTR human confirmation (LOC_STUDIO_MCP_HUMAN_CONFIRM=ask) over the REAL pinned SDK
      client (2026-07-28, in-process): the person is asked with the plan's summary; yes
      runs, no refuses and burns the plan; require refuses a client without elicitation;
      off (default) never asks

ASCII-only prints; final line is 'verify_mcp_plan_files OK'.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcpplan_")

_MCP_DIR = os.path.join(_ROOT, "integrations", "mcp-server")
sys.path.insert(0, _MCP_DIR)
os.environ.setdefault("LOC_STUDIO_TOKEN", "stdio-env-token")  # pregate: allow-secret fake

import loc_file_guard as G
import loc_plan_binding as PB
import registry
import server as S

_TMP = Path(tempfile.mkdtemp(prefix="mcpplan_"))
_ALLOWED = _TMP / "allowed"
_OUTSIDE = _TMP / "outside"
for d in (_ALLOWED, _OUTSIDE, _ALLOWED / ".aws", _ALLOWED / "sub"):
    d.mkdir(parents=True, exist_ok=True)


def _run(coro):
    return asyncio.run(coro)


def _refused(fn, needle: str, what: str) -> str:
    try:
        fn()
    except (S.ToolError, G.FileRefused, PB.PlanRefused) as exc:
        msg = str(exc)
        assert needle.lower() in msg.lower(), f"{what}: expected {needle!r} in {msg!r}"
        return msg
    raise AssertionError(f"{what}: was not refused")


class _NoApi:
    """Patch server._client so ANY /api call fails the test."""

    def __enter__(self):
        self.saved = S._client

        def boom(*a, **k):
            raise AssertionError("an /api call was made before the refusal")

        S._client = boom
        return self

    def __exit__(self, *exc):
        S._client = self.saved


def check_files() -> None:
    S._allow_local_file_reads(True, roots=[str(_ALLOWED)])
    good = _ALLOWED / "sub" / "hello.txt"
    good.write_bytes(b"Hello\nWorld\n")
    secret = _OUTSIDE / "credentials.txt"
    secret.write_bytes(b"aws_secret_access_key = x\n")  # pregate: allow-secret fixture
    (_ALLOWED / ".env.txt").write_bytes(b"K=1\n")
    (_ALLOWED / ".aws" / "credentials.txt").write_bytes(b"K=1\n")
    (_ALLOWED / "notes.exe").write_bytes(b"MZ")
    (_ALLOWED / "noext").write_bytes(b"x")

    msg = _refused(
        lambda: S._read_local_file(str(secret), "file", ("txt",)), "outside", "F1 outside"
    )
    assert str(_ALLOWED.resolve()) in msg and "LOC_STUDIO_INPUT_DIRS" in msg, msg
    _refused(
        lambda: S._read_local_file(str(_ALLOWED / ".env.txt"), "file", ("txt",)), "hidden", "F2"
    )
    _refused(
        lambda: S._read_local_file(str(_ALLOWED / ".aws" / "credentials.txt"), "file", ("txt",)),
        "hidden",
        "F2 dot-dir",
    )
    print("  F1/F2 outside folder, dotfile and dot-directory refused")

    link = _ALLOWED / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        print("  F3 (skipped: this OS user cannot create symlinks)")
    else:
        _refused(lambda: S._read_local_file(str(link), "file", ("txt",)), "outside", "F3 symlink")
        print("  F3 symlink inside an allowed folder pointing outside it refused")
    # A directory link (an NTFS junction on Windows, which needs no privilege; a directory
    # symlink elsewhere) inside an allowed folder that points outside it.
    jdir = _ALLOWED / "jdir"
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(_OUTSIDE), str(jdir))
        else:
            jdir.symlink_to(_OUTSIDE, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        print("  F3 (skipped: cannot create a directory link here)")
    else:
        _refused(
            lambda: S._read_local_file(str(jdir / "credentials.txt"), "file", ("txt",)),
            "outside",
            "F3 junction",
        )
        print("  F3 junction / directory link inside an allowed folder pointing outside refused")

    _refused(
        lambda: S._read_local_file(str(_ALLOWED / "notes.exe"), "file", ("txt",)), ".txt", "F4"
    )
    _refused(
        lambda: S._read_local_file(str(_ALLOWED / "noext"), "file", ("txt",)), "extension", "F4"
    )
    _refused(lambda: S._read_local_file(str(good), "glossary file"), "extension", "F4 glossary")
    _refused(lambda: S._read_local_file(str(good), "image"), ".png", "F4 image")
    _refused(lambda: S._read_local_file("relative/x.txt", "file", ("txt",)), "absolute", "F4 rel")
    print("  F4 extensions outside the route's list (or none) refused")

    os.environ[G.ENV_MAX] = "5"
    try:
        _refused(lambda: S._read_local_file(str(good), "file", ("txt",)), "over the 5-byte", "F5")
    finally:
        del os.environ[G.ENV_MAX]
    assert S._IMAGE_MAX_BYTES == 8 * 1024 * 1024
    print("  F5 size cap enforced before reading (images capped at 8 MiB)")

    S._allow_local_file_reads(True)  # env/default roots from here
    os.environ[G.ENV_DIRS] = os.pathsep.join([str(_OUTSIDE), "relative/dir", str(_TMP / "nope")])
    try:
        S.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        roots = G.configured_roots(S.OUTPUT_DIR)
        assert _OUTSIDE.resolve() in roots and _ALLOWED.resolve() not in roots, roots
        assert all(r.is_absolute() for r in roots), roots
        assert S.OUTPUT_DIR.resolve() in roots, "the output dir is always allowed"
        assert S._read_local_file(str(secret), "file", ("txt",)).startswith(b"aws_")
        _refused(lambda: S._read_local_file(str(good), "file", ("txt",)), "outside", "F6")
    finally:
        del os.environ[G.ENV_DIRS]
    home = Path.home()
    defaults = G.default_roots()
    assert home / "Documents" in defaults and home / "Downloads" in defaults, defaults
    print("  F6 LOC_STUDIO_INPUT_DIRS replaces Documents/Downloads; output dir always allowed")

    S._allow_local_file_reads(True, roots=[str(_ALLOWED)])
    with _NoApi():
        _refused(
            lambda: _run(S.submit_translation(str(secret), ["de-DE"], confirm=True)),
            "outside",
            "F7 submit",
        )
        plan = _run(S._plan_submit_translation({"file_path": str(secret), "targets": ["de-DE"]}))
    assert "source_bytes" not in plan["scope"], plan
    assert any("outside" in w for w in plan["warnings"]), plan["warnings"]
    print("  F7 submit_translation refuses before any /api call; its plan only warns")

    assert S._read_local_file(str(good), "file", ("txt",)) == b"Hello\nWorld\n"
    plan = _run(S._plan_submit_translation({"file_path": str(good), "targets": ["de-DE"]}))
    assert plan["scope"]["source_bytes"] == 12, plan
    print("  F8 a permitted file reads byte-identical")
    S._allow_local_file_reads(False)


# ----------------------------------------------------------------------------- #757
def check_schema() -> None:
    for tool in registry.TOOLS:
        props = (tool.input_schema or {}).get("properties") or {}
        if tool.tier == "spend":
            assert props.get("plan_id", {}).get("type") == "string", tool.name
            assert "confirm" in props, tool.name
        else:
            assert "plan_id" not in props, tool.name
    print("  P1 plan_id on every spend tool's schema, on no other tool")


class _Stub:
    def __init__(self):
        self.calls: list = []

    async def handler(self, **kw):
        self.calls.append(kw)
        return json.dumps({"ran": True})


async def _stub_planner(kw):
    n = len(kw.get("targets") or [])
    return {"what_would_happen": f"Translate into {n} locale(s)."}


def _dispatch(stub, name, args):
    spec = registry.tool_by_name(name)
    return _run(S._dispatch(spec, stub.handler, dict(args)))


def check_binding() -> None:
    saved = dict(S.PLANNERS)
    S.PLANNERS["submit_translation"] = _stub_planner
    S.PLANNERS["translate_text"] = _stub_planner
    stub = _Stub()
    three = {"file_path": "/x.txt", "targets": ["de-DE", "fr-FR", "ja-JP"]}
    try:
        plan = json.loads(_dispatch(stub, "submit_translation", three))
        pid = plan["plan_id"]
        assert plan["plan"] is True and pid.startswith("plan_") and len(pid) > 20, plan
        assert pid in plan["next_step"] and plan["plan_expires_in_s"] == 1800, plan
        assert not stub.calls

        def refused(args, needle, what, name="submit_translation"):
            _refused(lambda: _dispatch(stub, name, args), needle, what)
            assert not stub.calls, f"{what}: the handler ran"

        refused({**three, "confirm": True}, "no plan_id", "P2 no plan_id")
        print("  P2 plan returns plan_id; confirm without it is spend_refused")

        nine = ["de-DE", "fr-FR", "ja-JP", "es-ES", "it-IT", "pt-BR", "ko-KR", "zh-CN", "nl-NL"]
        msg = _refused(
            lambda: _dispatch(
                stub,
                "submit_translation",
                {**three, "targets": nine, "confirm": True, "plan_id": pid},
            ),
            "spend_refused",
            "P3 nine locales",
        )
        assert "changed: targets" in msg, msg
        refused(
            {**three, "project_id": 7, "confirm": True, "plan_id": pid}, "project_id", "P3 +arg"
        )
        other = json.loads(_dispatch(stub, "translate_text", {"text": "hi", "target_locale": "de"}))
        refused(
            {**three, "confirm": True, "plan_id": other["plan_id"]}, "translate_text", "P3 tool"
        )
        refused({**three, "confirm": True, "plan_id": "plan_forged"}, "not a plan", "P3 forged")
        # another caller (an HTTP request with its own token) cannot use this plan
        tok = S._REQUEST_TOKEN.set("someone-else")
        try:
            refused({**three, "confirm": True, "plan_id": pid}, "not a plan", "P3 caller")
        finally:
            S._REQUEST_TOKEN.reset(tok)
        # expiry
        old = PB.time.monotonic
        PB.time.monotonic = lambda: old() + 3600
        try:
            refused({**three, "confirm": True, "plan_id": pid}, "expired", "P3 expired")
        finally:
            PB.time.monotonic = old

        out = json.loads(
            _dispatch(stub, "submit_translation", {**three, "confirm": True, "plan_id": pid})
        )
        assert out == {"ran": True} and len(stub.calls) == 1, (out, stub.calls)
        assert "plan_id" not in stub.calls[0] and stub.calls[0]["confirm"] is True, stub.calls
        stub.calls.clear()
        refused({**three, "confirm": True, "plan_id": pid}, "already used", "P3 reuse")
        # an omitted optional argument equals an explicit null (FastMCP fills defaults)
        plan2 = json.loads(_dispatch(stub, "submit_translation", three))
        _dispatch(
            stub,
            "submit_translation",
            {**three, "project_id": None, "confirm": True, "plan_id": plan2["plan_id"]},
        )
        assert len(stub.calls) == 1
        print("  P3 changed args / other tool / forged / other caller / expired / reused refused")
        print("  P4 the matching plan_id runs the handler once, without plan_id in its kwargs")

        # P6 (merge with m-jobs #747): idempotency_key is a retry token, not part of the plan.
        # Planned without a key, confirmed with one; a replay of the SAME plan with the SAME
        # key reaches the handler (the API dedups the key, nothing is spent twice); a replay
        # with another key or none is refused; the key reaches the handler's request.
        stub.calls.clear()
        plan3 = json.loads(_dispatch(stub, "submit_translation", three))
        go = {**three, "confirm": True, "plan_id": plan3["plan_id"], "idempotency_key": "k-1"}
        seen_keys: list = []
        real_handler = stub.handler

        async def keyed(**kw):
            seen_keys.append(S.longrun.IDEMPOTENCY_KEY.get())
            return await real_handler(**kw)

        spec = registry.tool_by_name("submit_translation")
        _run(S._dispatch(spec, keyed, dict(go)))
        _run(S._dispatch(spec, keyed, dict(go)))  # the retry after a lost answer
        assert len(stub.calls) == 2 and seen_keys == ["k-1", "k-1"], (stub.calls, seen_keys)
        stub.calls.clear()
        refused({**go, "idempotency_key": "k-2"}, "already used", "P6 other key")
        refused({k: v for k, v in go.items() if k != "idempotency_key"}, "already used", "P6 none")
        refused({**go, "targets": nine}, "changed: targets", "P6 replay changed args")
        print("  P6 plan_id + idempotency_key: same-key replay passes; other key / none refused")

        store = PB.PlanStore(max_plans=3)
        ids = [store.issue("t", {"a": i}, "c") for i in range(5)]
        assert len(store) == 3
        _refused(lambda: store.redeem("t", {"a": 0}, "c", ids[0]), "not a plan", "P bounded")
        store.redeem("t", {"a": 4}, "c", ids[4])
    finally:
        S.PLANNERS.clear()
        S.PLANNERS.update(saved)


def check_mrtr() -> None:
    """The REAL pinned SDK client, in-process, at 2026-07-28 (MRTR)."""
    import mcp_types as types
    from mcp.client.client import Client

    stub = _Stub()
    saved_h, saved_p = dict(S.HANDLERS), dict(S.PLANNERS)
    S.HANDLERS["translate_text"] = stub.handler
    S.PLANNERS["translate_text"] = _stub_planner
    srv = S._LocMCPServer("plan-test")
    srv.middleware.append(S._bind_request_middleware)
    S.register_tools(srv)
    asked: list = []
    answer = {"action": "accept", "content": {"approve": True}}

    async def eli(ctx, params):
        asked.append(params.message)
        return types.ElicitResult(**answer)

    args = {"text": "hi", "target_locale": "de-DE"}

    async def flow(callback):
        kw = {"elicitation_callback": callback} if callback else {}
        async with Client(srv, **kw) as c:
            r = await c.call_tool("translate_text", args)
            pid = json.loads(r.content[0].text)["plan_id"]
            r = await c.call_tool("translate_text", {**args, "confirm": True, "plan_id": pid})
            text = "".join(getattr(b, "text", "") for b in r.content)
            return pid, r.is_error, text

    try:
        os.environ[S._HUMAN_CONFIRM_ENV] = "ask"
        _pid, err, text = _run(flow(eli))
        assert not err and json.loads(text) == {"ran": True}, text
        assert len(asked) == 1 and "Translate into" in asked[0] and "Approve" in asked[0], asked
        assert len(stub.calls) == 1, stub.calls

        asked.clear()
        stub.calls.clear()
        answer.update(action="accept", content={"approve": False})
        pid, err, text = _run(flow(eli))
        assert err and "declined" in text and not stub.calls, text
        msg = _refused(
            lambda: _run(
                S._dispatch(
                    registry.tool_by_name("translate_text"),
                    stub.handler,
                    {**args, "confirm": True, "plan_id": pid},
                )
            ),
            "already used",
            "P5 a no burns the plan",
        )
        assert msg

        answer.update(action="decline", content=None)
        _pid, err, text = _run(flow(eli))
        assert err and "declined" in text and not stub.calls, text
        print("  P5 MRTR: the person is asked (real SDK, 2026-07-28); yes runs, no/decline refuse")

        asked.clear()
        _pid, err, text = _run(flow(None))  # a client that declares no elicitation
        assert not err and len(stub.calls) == 1 and not asked, text
        stub.calls.clear()
        os.environ[S._HUMAN_CONFIRM_ENV] = "require"
        _pid, err, text = _run(flow(None))
        assert err and "cannot show an approval prompt" in text and not stub.calls, text
        os.environ[S._HUMAN_CONFIRM_ENV] = "off"
        answer.update(action="accept", content={"approve": True})
        _pid, err, text = _run(flow(eli))
        assert not err and not asked and len(stub.calls) == 1, (text, asked)
        print("  P5 ask runs on a client without elicitation; require refuses it; off never asks")
    finally:
        os.environ.pop(S._HUMAN_CONFIRM_ENV, None)
        S.HANDLERS.clear()
        S.HANDLERS.update(saved_h)
        S.PLANNERS.clear()
        S.PLANNERS.update(saved_p)


def main() -> None:
    check_files()
    check_schema()
    check_binding()
    check_mrtr()
    print("verify_mcp_plan_files OK")


if __name__ == "__main__":
    main()
