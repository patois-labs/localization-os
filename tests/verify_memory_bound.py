"""Offline memory accounting and admission ratchets for #109. Runner entrypoint only."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._api_cli_patch import patch_api_cli
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_memory_")

from fastapi.testclient import TestClient

from app import config, db
from app.main import app
from tests import verify_stress_liveness_pool as stress


def test_budget_boot():
    for budget, success in (("1", False), ("8192", True), ("", True)):
        env = dict(os.environ, LOC_TMS_MEMORY_BUDGET_MB=budget)
        result = subprocess.run(
            [sys.executable, "-c", "from app import config"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (result.returncode == 0) == success, (budget, result.stderr)
        if not success:
            assert "LOC_TMS_MEMORY_BUDGET_MB" in result.stderr
    with patch.object(config, "MEMORY_BUDGET_MB", 1):
        try:
            config.assert_memory_budget()
            raise AssertionError("arithmetic over budget was accepted")
        except ValueError:
            pass


def test_job_workers_and_config_list():
    from tests._cli_runner import run_cli

    result = subprocess.run(
        [sys.executable, "-c", "from app.services import jobs; print(jobs.WORKER_COUNT)"],
        env=dict(os.environ, LOC_TMS_JOB_WORKERS="3"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0 and result.stdout.strip() == "3", result.stderr
    code, out, err = run_cli(["--json", "config", "list", "--group", "capacity"])
    assert code == 0, (out, err)
    values = json.loads(out)["settings"]
    assert values["LOC_TMS_ANYIO_WORKERS"] == config.ANYIO_WORKERS
    assert values["LOC_TMS_STREAM_WORKERS"] == config.STREAM_WORKERS
    assert values["LOC_TMS_STREAM_WAIT_S"] == config.STREAM_WAIT_S
    assert values["LOC_TMS_RUN_WORKERS"] == config.RUN_WORKERS
    assert values["LOC_TMS_JOB_WORKERS"] == config.JOB_WORKERS
    assert values["LOC_TMS_MEMORY_BUDGET_MB"] == config.MEMORY_BUDGET_MB


def test_worker_limiter():
    import anyio
    from anyio import to_thread

    async def exercise():
        limiter = to_thread.current_default_thread_limiter()
        assert limiter.total_tokens == 2, limiter.total_tokens
        entered = []
        release = threading.Event()

        def block(i):
            entered.append(i)
            assert release.wait(10)

        tasks = [asyncio.create_task(to_thread.run_sync(block, i)) for i in range(3)]
        try:
            with anyio.fail_after(5):
                while len(entered) < 2:
                    await anyio.sleep(0.01)
            await anyio.sleep(0.1)
            assert len(entered) == 2, entered
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert sorted(entered) == [0, 1, 2]

    with patch.object(config, "ANYIO_WORKERS", 2, create=True), TestClient(app) as client:
        client.portal.call(exercise)


def test_child_workers():
    from app.services import orchestrator, settings, translate

    with patch.object(config, "RUN_WORKERS", 2, create=True):
        settings.set_many({"mt_concurrency": "16"})
        try:
            assert orchestrator._mt_workers() == 2
            assert translate._mt_workers() == 2
        finally:
            settings.set_many({"mt_concurrency": "4"})


def test_sync_admission():
    from sqlmodel import Session

    from app.services import orchestrator

    held = orchestrator.RUN_SLOTS.take(config.MAX_CONCURRENT_RUNS)
    try:
        with (
            Session(db.engine) as session,
            patch_pieces(
                "app.services.orchestrator",
                "_run_workflow_stream_impl",
                side_effect=AssertionError("execution bypassed admission"),
            ),
        ):
            try:
                list(orchestrator.run_workflow_stream(session, 1, 1, "es-ES"))
                raise AssertionError("sync launch did not refuse")
            except orchestrator.RunCapacityError:
                pass
    finally:
        for token in held:
            token.release()

    # Opposite arms: an owned token survives its child, and close/error returns
    # a synchronous reservation exactly once. No database work in this small double.
    with (
        Session(db.engine) as session,
        patch_pieces(
            "app.services.orchestrator",
            "_run_workflow_stream_finalized",
            return_value=iter([{"type": "probe"}]),
        ),
    ):
        stream = orchestrator.run_workflow_stream(session, 1, 1, "es-ES")
        next(stream)
        assert orchestrator.RUN_SLOTS.in_flight == 0, "reservation retained across caller yield"
        stream.close()
        assert orchestrator.RUN_SLOTS.in_flight == 0
    with (
        Session(db.engine) as session,
        patch_pieces(
            "app.services.orchestrator",
            "_run_workflow_stream_finalized",
            side_effect=ValueError("fixture"),
        ),
    ):
        try:
            list(orchestrator.run_workflow_stream(session, 1, 1, "es-ES"))
        except ValueError:
            pass
        else:
            raise AssertionError("execution error was swallowed")
        assert orchestrator.RUN_SLOTS.in_flight == 0


def test_reserved_batch_and_resume():
    from app.services import batch_runs, orchestrator

    with patch.object(config, "MAX_CONCURRENT_RUNS", 1):
        token = orchestrator.RUN_SLOTS.take()[0]
        try:
            with patch_pieces(
                "app.services.orchestrator", "_run_workflow_stream_finalized", return_value=iter(())
            ) as execute:
                batch_runs._run_member(db.engine, 1, 1, 1, "es-ES", slot=token)
                assert execute.call_count == 1, "batch child acquired a second slot"
                assert orchestrator.RUN_SLOTS.in_flight == 1

            def resumed(*args):
                assert orchestrator.RUN_SLOTS.in_flight == 2
                try:
                    with orchestrator._execution_slot():
                        raise AssertionError("fresh launch admitted at full cap")
                except orchestrator.RunCapacityError:
                    pass
                return {"status": "completed"}

            with patch_pieces(
                "app.services.orchestrator", "_resume_workflow_admitted", side_effect=resumed
            ):
                assert (
                    orchestrator.resume_workflow(None, 1, "es-ES", "edited")["status"]
                    == "completed"
                )
                from types import SimpleNamespace

                from app.models import RunStatus
                from app.services import audit, notifications, vendor_return

                run = SimpleNamespace(
                    id=1, status=RunStatus.PAUSED, context={}, target_locale="es-ES"
                )
                session = Mock()
                session.get.return_value = run
                with (
                    patch.object(audit, "log") as logged,
                    patch.object(notifications, "on_vendor_return_resume_failed") as notified,
                ):
                    assert vendor_return._resume_leg(
                        session, SimpleNamespace(id=1), run, ["edited"], actor="test", mode="sync"
                    )
                    logged.assert_not_called()
                    notified.assert_not_called()
                assert orchestrator.RUN_SLOTS.in_flight == 1
        finally:
            token.release()


def test_sqlite_mmap():
    # Run the real connection initializer on a connection whose mmap was enabled.
    import sqlite3

    with closing(sqlite3.connect(config.DB_PATH)) as conn:
        conn.execute("PRAGMA mmap_size=1048576")
        db._sqlite_pragmas(conn, None)
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 0
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == -config.SQLITE_CACHE_KB


def test_stream_isolation():
    import anyio
    import httpx
    from fastapi import FastAPI

    from app.web.workflows import StreamingResponse

    async def exercise():
        default = anyio.to_thread.current_default_thread_limiter()
        old = default.total_tokens
        default.total_tokens = 1
        release = threading.Event()
        entered = []
        mini = FastAPI()

        def body():
            entered.append(1)
            assert release.wait(5)
            yield b"event\n"

        @mini.get("/stream")
        async def stream():
            return StreamingResponse(body())

        @mini.get("/api/ping")
        def ping():
            return {"ok": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mini), base_url="http://test"
        ) as client:
            tasks = [asyncio.create_task(client.get("/stream")) for _ in range(3)]
            try:
                with anyio.fail_after(2):
                    while len(entered) < 2:
                        await anyio.sleep(0.01)
                await anyio.sleep(0.1)
                assert default.borrowed_tokens == 0, "stream holds a default-limiter token"
                assert len(entered) == 2, "dedicated stream limiter not enforced"
                with anyio.fail_after(1):
                    assert (await client.get("/api/ping")).json() == {"ok": True}
            finally:
                release.set()
                await asyncio.gather(*tasks)
                default.total_tokens = old

    with patch.object(config, "STREAM_WORKERS", 2, create=True):
        asyncio.run(exercise())
    # Every streaming surface must use the same adapter, including local imports.
    import ast

    for path in Path("app").rglob("*.py"):
        if path.name == "streaming.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and any(
                n.name in {"StreamingResponse", "EventSourceResponse"} for n in node.names
            ):
                assert node.module == "app.web.streaming", str(path)


def test_slot_release_wakes():
    from app.services import orchestrator

    with patch.object(orchestrator.ADMISSION, "notify") as notify:
        with orchestrator._execution_slot() as token:
            assert not token.handed_off
        notify.assert_called_once()
        assert not token.handed_off, "release must not forge an ownership transfer"


def test_abandoned_stream():
    from app.services import orchestrator

    closed = []

    def body(*args, **kwargs):
        try:
            assert orchestrator.RUN_SLOTS.in_flight == 1
            yield {"type": "probe"}
            assert orchestrator.RUN_SLOTS.in_flight == 1
            yield {"type": "probe"}
        finally:
            closed.append(True)

    with patch_pieces("app.services.orchestrator", "_run_workflow_stream_finalized", body):
        stream = orchestrator.run_workflow_stream(None, 1, 1, "es-ES")
        cycle = [stream]
        cycle.append(cycle)
        next(stream)
        assert orchestrator.RUN_SLOTS.in_flight == 0, "abandoned yield retains slot until GC"
        next(stream)
        stream.close()
        assert closed == [True]
        assert orchestrator.RUN_SLOTS.in_flight == 0


def test_sync_summary_ownership():
    from app.services import orchestrator

    original = orchestrator.run_workflow_stream

    def observed(*args, **kwargs):
        with closing(original(*args, **kwargs)) as stream:
            for event in stream:
                assert orchestrator.RUN_SLOTS.in_flight == 1, "sync summary lost its parent slot"
                yield event

    def body(*args, **kwargs):
        assert orchestrator.RUN_SLOTS.in_flight == 1, "sync summary acquired a second slot"
        yield {"type": "done", "text": "ok"}

    with (
        patch_pieces("app.services.orchestrator", "_refuse_archived"),
        patch_pieces("app.services.orchestrator", "_refuse_project_budget"),
        # #213 part 2: the cross-workflow conflict guard reads the session; this cell
        # passes a Mock() and tests SLOT ownership, so it is stubbed exactly like the
        # launch guards above (the guard's own behaviour is exercised for real in
        # verify_run_recovery_progress / verify_api_workflow_run_async).
        patch_pieces("app.services.orchestrator", "_refuse_overlapping_run"),
        patch_pieces("app.services.orchestrator", "run_workflow_stream", observed),
        patch_pieces(
            "app.services.orchestrator",
            "_run_workflow_stream_finalized",
            side_effect=body,
        ),
    ):
        assert orchestrator.run_workflow(Mock(), 1, 1, "es-ES")["text"] == "ok"
    assert orchestrator.RUN_SLOTS.in_flight == 0


def test_unknown_selection():
    with patch.dict(os.environ, MEMORY_CHECK="not_a_check"):
        try:
            main()
        except ValueError as exc:
            assert "test_unknown_selection" in str(exc), "known checks not listed"
        else:
            raise AssertionError("unknown MEMORY_CHECK ran zero checks and succeeded")


def test_cli_import_failure():
    import builtins

    from app.cli import __main__ as cli

    original_import = builtins.__import__

    def guard(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app.kernel" and "orchestrator" in fromlist:
            raise ImportError("broken orchestrator install")
        return original_import(name, globals, locals, fromlist, level)

    with (
        patch.object(cli, "_feature_dispatch_gate", side_effect=ValueError("original failure")),
        patch("builtins.__import__", guard),
    ):
        try:
            cli.main(["config", "list"])
        except ValueError as exc:
            assert str(exc) == "original failure"
        else:
            raise AssertionError("the original exception was swallowed")


def test_mcp_structured_refusal():
    import httpx

    spec = importlib.util.spec_from_file_location(
        "memory_low_mcp", Path("integrations/mcp-server/server.py")
    )
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    for field in ("code", "reason"):
        response = httpx.Response(
            503,
            json={field: "run_capacity", "error": "A revised shared refusal.", "retry_after": 2},
        )
        assert relay._explain_status(response) == "A revised shared refusal."


def test_sqlite_connection_closed():
    import sqlite3

    opened = []
    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        conn = connect(*args, **kwargs)
        opened.append(conn)
        return conn

    with patch.object(sqlite3, "connect", tracked):
        test_sqlite_mmap()
    try:
        for conn in opened:
            try:
                conn.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                pass
            else:
                raise AssertionError("sqlite test connection was not closed")
    finally:
        for conn in opened:
            conn.close()


def test_capacity_masking():
    from app.cli import cmd_config
    from tests._cli_runner import run_cli

    with (
        patch.dict(cmd_config._GROUPS, capacity=["LOC_TMS_ANYIO_WORKERS", "LOC_TMS_UNKNOWN"]),
        patch.object(cmd_config, "is_secret_key", return_value=True),
    ):
        code, out, err = run_cli(["--json", "config", "list", "--group", "capacity"])
        assert code == 0, (out, err)
        assert json.loads(out)["settings"] == {"LOC_TMS_ANYIO_WORKERS": "(set)"}


def test_capacity_docs():
    surfaces = (
        "docs/architecture/configuration.md",
        "docs/DEPLOY.md",
        "docs/CLI.md",
        "app/templates/help.html",
        "app/capabilities/llm.md",
        "app/templates/run_view.html",
    )
    for filename in surfaces:
        text = Path(filename).read_text(encoding="utf-8")
        assert "LOC_TMS_STREAM_WORKERS" in text, filename
        assert "streams and long waits are off" in text.lower(), filename
        assert "LOC_TMS_STREAM_WAIT_S" in text and "stream_capacity" in text, filename
        assert "one token per open SSE tab or waiting caller" in text, filename
        if filename in surfaces[:2]:
            rows = [line for line in text.splitlines() if line.startswith("|")]
            assert any("unmeasured planning reserve" in row for row in rows), filename
            assert any("46 to 50 MiB" in row for row in rows), filename
            fly_row = next(row for row in rows if "1 GB shared-cpu VM" in row)
            assert "| 8 | 20 + 40 |" in fly_row, fly_row
            assert "until a measured profile exists" in text, filename
            profile_table = text[text.index(fly_row) :].split("\n\n", 1)[0].splitlines()
            assert len(profile_table) == 4, profile_table
            for row in profile_table:
                assert row.split("|")[5].split("/")[1].strip() == "32", row
    architecture = Path(surfaces[0]).read_text(encoding="utf-8")
    assert "including synchronous workflow execution and resume" not in architecture
    assert "within one process" in architecture
    assert "app/web/streaming.py::StreamingResponse" in architecture
    assert "32 x 8 MiB = 256 MiB" in architecture
    # 2026-09-14 SRP split: the orchestrator lives in orchestrator.py plus sibling pieces
    orchestrator = "".join(
        f.read_text(encoding="utf-8") for f in sorted(Path("app/services").glob("orchestrator*.py"))
    )
    health = Path("app/web/health.py").read_text(encoding="utf-8")
    assert "40, not overridden" not in orchestrator
    assert "iterate_in_threadpool" not in orchestrator
    assert "Streams and long waits are off" in orchestrator
    assert "same forty threads" not in health and "never forty" not in health


def test_round3_wait_capacity():
    import anyio
    import httpx

    from app.web import api_cli

    wid, did = stress._mk_wf("mock"), stress._mk_doc("es-ES", 2)
    headers = {"X-Loc-Token": stress._api_key()}
    spec = importlib.util.spec_from_file_location(
        "memory_wait_mcp", "integrations/mcp-server/server.py"
    )
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    release = threading.Event()
    entered = []
    original = api_cli._drain_run_to_terminal

    def drain(run_id):
        entered.append(run_id)
        assert release.wait(10)
        return original(run_id)

    async def exercise():
        default = anyio.to_thread.current_default_thread_limiter()
        old = default.total_tokens
        default.total_tokens = 4

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
            )

        async with client() as http:
            path = f"/api/workflows/{wid}/run"
            body = {"doc_id": did, "locale": "es-ES", "wait": True}
            tasks = [asyncio.create_task(http.post(path, json=body)) for _ in range(2)]
            try:
                with anyio.fail_after(4):
                    while len(entered) < 2:
                        await anyio.sleep(0.01)
                assert default.statistics().borrowed_tokens == 0, "long waits borrow default tokens"
                start = time.monotonic()
                with anyio.fail_after(config.STREAM_WAIT_S + 1):
                    extra = await http.post(path, json=body)
                assert time.monotonic() - start < config.STREAM_WAIT_S + 1
                out = extra.json()
                assert extra.status_code == 200 and out["waited"] is False, out
                assert out["retry_after"] > 0 and out["run_id"] in entered
                from sqlmodel import Session

                from app.models import WorkflowRun

                with Session(db.engine) as session:
                    assert out["status"] == str(session.get(WorkflowRun, out["run_id"]).status)
                with anyio.fail_after(1):
                    assert (await http.get("/api/languages")).status_code == 200
                with (
                    patch.object(relay, "_client", client),
                    patch.object(relay, "TOKEN", headers["X-Loc-Token"]),
                    patch.object(relay, "_WORKFLOW_WAIT_S", 0.2),
                ):
                    result = json.loads(
                        await relay.run_workflow("run", wid, did, "es-ES", confirm=True)
                    )
                    assert result["done"] is False and "work_status" in result["next"], result
                    status = json.loads(await relay.work_status(run_id=result["run_id"]))
                    assert status["status"] == out["status"], status
                assert default.statistics().borrowed_tokens == 0
            finally:
                release.set()
                await asyncio.gather(*tasks)
                default.total_tokens = old

    with (
        patch.object(config, "STREAM_WORKERS", 2),
        patch.object(config, "STREAM_WAIT_S", 0.1),
        patch_api_cli("_drain_run_to_terminal", drain),
        patch_api_cli("_WAIT_POLL_CEILING_S", 0.1),
        patch_pieces("app.services.orchestrator", "_run_in_background"),
    ):
        asyncio.run(exercise())


def test_round3_stream_capacity():
    import anyio
    import httpx
    from fastapi import FastAPI

    from app.web.streaming import StreamingResponse

    assert config.STREAM_WORKERS == 32, "default must allow 32 watched tabs/waits"
    assert config.STREAM_WAIT_S == 5

    async def exercise():
        mini = FastAPI()
        entered, closed = [], []
        release = threading.Event()

        def body():
            try:
                entered.append(True)
                assert release.wait(5)
                yield b"event\n"
            finally:
                closed.append(True)

        @mini.get("/stream")
        async def stream():
            return StreamingResponse(body(), media_type="text/event-stream")

        @mini.get("/ping")
        def ping():
            return {"ok": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mini), base_url="http://test"
        ) as client:
            tasks = [asyncio.create_task(client.get("/stream")) for _ in range(2)]
            try:
                with anyio.fail_after(2):
                    while len(entered) < 2:
                        await anyio.sleep(0.01)
                with anyio.fail_after(config.STREAM_WAIT_S + 1):
                    refused = await client.get("/stream")
                    assert refused.status_code == 503, refused.text
                    assert refused.json()["code"] == "stream_capacity"
                    assert int(refused.headers["Retry-After"]) > 0
                    assert (await client.get("/ping")).status_code == 200
                assert (
                    anyio.to_thread.current_default_thread_limiter().statistics().borrowed_tokens
                    == 0
                )
                from app.cli._common import CliError
                from app.cli._remote import RemoteClient

                try:
                    RemoteClient.__new__(RemoteClient)._decode(refused)
                except CliError as exc:
                    assert str(exc) == refused.json()["error"]
                else:
                    raise AssertionError("CLI accepted stream refusal")
                spec = importlib.util.spec_from_file_location(
                    "memory_stream_mcp", "integrations/mcp-server/server.py"
                )
                relay = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(relay)
                assert relay._explain_status(refused) == refused.json()["error"]
            finally:
                release.set()
                await asyncio.gather(*tasks)
            assert len(closed) == 2

    with patch.object(config, "STREAM_WORKERS", 2), patch.object(config, "STREAM_WAIT_S", 0.1):
        asyncio.run(exercise())


def test_round3_cleanup():
    import anyio

    from app.web import streaming

    async def exercise():
        closed = []
        limiter = anyio.CapacityLimiter(2)
        streaming._limiter.set(limiter)
        owner = []

        def body():
            from sqlmodel import Session

            try:
                with Session(db.engine) as session:
                    owner.append(threading.get_ident())
                    from sqlalchemy import text

                    session.exec(text("SELECT 1"))
                    yield b"one"
            finally:
                closed.append(threading.get_ident())

        stream = streaming._iterate(body())
        await anext(stream)
        borrower = object()
        limiter.acquire_on_behalf_of_nowait(borrower)

        async def close():
            await stream.aclose()

        task = asyncio.create_task(close())
        try:
            done, _ = await asyncio.wait([task], timeout=0.5)
            assert done and closed, "cleanup queues behind stream limiter"
            assert closed == owner and closed[0] != threading.get_ident(), (
                "producer cleanup left its worker"
            )
        finally:
            limiter.release_on_behalf_of(borrower)
            await task

    asyncio.run(exercise())


def test_round4_mcp_poll():
    import httpx

    from app.services import providers
    from app.web import streaming

    spec = importlib.util.spec_from_file_location(
        "memory_round4_mcp", "integrations/mcp-server/server.py"
    )
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    assert hasattr(relay, "_WORKFLOW_WAIT_S"), "MCP needs a bounded polling budget"
    description = next(tool.summary for tool in relay.registry.TOOLS if tool.name == "run_workflow")
    assert 40 <= len(description) <= 320, description
    assert all(term in description for term in ("done:false", "work_status", "45 seconds")), (
        description
    )
    assert "wait=false" in description
    assert "wait=false" in relay.run_workflow.__doc__
    recipe = Path("integrations/mcp-server/skill/references/api-recipes.md").read_text(
        encoding="utf-8"
    )
    assert "`done:false`" in recipe and "45-second budget" in recipe
    assert "`wait=false`" in recipe
    headers = {"X-Loc-Token": stress._api_key()}
    providers.register_provider("barriermock", stress._Barrier())
    wid, did = stress._mk_wf("barriermock"), stress._mk_doc("es-ES", 2)

    async def exercise():
        limiter = streaming.stream_limiter()
        borrower = object()
        limiter.acquire_on_behalf_of_nowait(borrower)
        polls = []
        real_status = relay.work_status

        async def status(**kwargs):
            polls.append(kwargs)
            if len(polls) == 2:
                stress._Barrier.release.set()
            return await real_status(**kwargs)

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
            )

        try:
            with (
                patch.object(relay, "_client", client),
                patch.object(relay, "TOKEN", headers["X-Loc-Token"]),
            ):
                stress._Barrier.release.clear()
                with (
                    patch.object(relay, "_WORKFLOW_WAIT_S", 5),
                    patch.object(relay, "_POLL_GAP_S", 0.05),
                    patch.object(relay, "work_status", status),
                ):
                    result = json.loads(
                        await relay.run_workflow("run", wid, did, "es-ES", confirm=True)
                    )
                assert (
                    len(polls) >= 2 and result["done"] is True and result["status"] == "completed"
                ), result
                stress._Barrier.release.clear()
                other = stress._mk_doc("es-ES", 2)
                polls.clear()
                with (
                    patch.object(relay, "_WORKFLOW_WAIT_S", 0.15),
                    patch.object(relay, "work_status", wraps=real_status) as observed,
                ):
                    result = json.loads(
                        await relay.run_workflow("run", wid, other, "es-ES", confirm=True)
                    )
                assert observed.call_count >= 1, "MCP did not poll"
                assert result["done"] is False and result["status"] in {"running", "queued"}, result
                assert (
                    "work_status" in result["next"] and str(result["run_id"]) in result["next"]
                ), result
        finally:
            stress._Barrier.release.set()
            limiter.release_on_behalf_of(borrower)

    with patch.object(config, "STREAM_WORKERS", 1), patch.object(config, "STREAM_WAIT_S", 0.01):
        asyncio.run(exercise())
    assert stress._wait_slots_idle() == 0


def test_round4_wait_guards_and_headers():
    import anyio
    import httpx
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    from app.web import api_cli, streaming

    async def exercise():
        limiter = streaming.stream_limiter()
        borrower = object()
        limiter.acquire_on_behalf_of_nowait(borrower)
        try:
            with patch.object(
                type(limiter), "acquire_on_behalf_of", wraps=limiter.acquire_on_behalf_of
            ) as acquire:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    with anyio.fail_after(0.5):
                        response = await client.post("/api/workflows/1/run", json={"wait": True})
                assert response.status_code == 401, response.text
                assert acquire.call_count == 0, "anonymous wait contended stream capacity"
            request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
            request._body = b'{"wait":true}'
            for response in (
                JSONResponse({"ok": True}, headers={"X-Proof": "kept"}),
                Response(b"custom", media_type="text/plain"),
            ):

                async def handler(request, response=response):
                    return response

                with (
                    patch_api_cli("_gate", return_value=({}, None, None)),
                    patch_api_cli("_write_block", return_value=None),
                    patch.object(config, "STREAM_WAIT_S", 0.01),
                ):
                    result = await api_cli._stream_wait_route(handler)(request=request)
                if isinstance(response, JSONResponse):
                    assert result.headers["X-Proof"] == "kept"
                    assert int(result.headers["content-length"]) == len(result.body)
                else:
                    assert result is response

            await _batch_detail_refresh(("rerun",))
        finally:
            limiter.release_on_behalf_of(borrower)

    with patch.object(config, "STREAM_WORKERS", 1), patch.object(config, "STREAM_WAIT_S", 1):
        asyncio.run(exercise())


def test_round4_batch_poll():
    import httpx

    spec = importlib.util.spec_from_file_location(
        "memory_round4_batches", "integrations/mcp-server/server.py"
    )
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)

    async def exercise():
        for mode in ("run_batch", "rerun", "retry_failed"):
            for finish in (True, False):
                polls = []

                def forward(request, polls=polls, finish=finish, mode=mode):
                    if request.method == "POST":
                        return httpx.Response(
                            200, json={"ok": True, "id": 123, "status": "running", "waited": False}
                        )
                    assert request.url.path == "/api/batches/123"
                    if mode in {"rerun", "retry_failed"}:
                        assert request.url.params.get("detail") == "steps", request.url
                    polls.append(request.url.path)
                    return httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "id": 123,
                            "status": "completed" if finish else "running",
                            "members": [{"status": "completed" if finish else "running"}],
                        },
                    )

                def client():
                    return httpx.AsyncClient(
                        transport=httpx.MockTransport(forward), base_url="http://test"
                    )

                with (
                    patch.object(relay, "_client", client),
                    patch.object(relay, "_WORKFLOW_WAIT_S", 0.05),
                    patch.object(relay, "TOKEN", "offline-test-token"),
                ):
                    out = json.loads(
                        await relay.run_workflow(
                            mode,
                            workflow_id=1,
                            document_id=2,
                            batch_id=123,
                            locales=["es-ES"],
                            confirm=True,
                        )
                    )
                assert polls and out["done"] is finish, (mode, out)
                assert out["members"][0]["status"] == out["status"]
                if not finish:
                    assert "work_status" in out["next"] and "batch_id=123" in out["next"], out

        from app.web import streaming

        limiter = streaming.stream_limiter()
        borrower = object()
        limiter.acquire_on_behalf_of_nowait(borrower)
        try:
            await _batch_detail_refresh(("retry-failed",))
        finally:
            limiter.release_on_behalf_of(borrower)

    with patch.object(config, "STREAM_WORKERS", 1), patch.object(config, "STREAM_WAIT_S", 0.01):
        asyncio.run(exercise())


async def _batch_detail_refresh(modes):
    from datetime import datetime

    import httpx
    from sqlmodel import Session

    from app.models import WorkflowBatch, WorkflowRun

    headers = {"X-Loc-Token": stress._api_key()}
    wid, did = stress._mk_wf("mock"), stress._mk_doc("es-ES", 2)
    for mode in modes:
        with Session(db.engine) as session:
            batch = WorkflowBatch(
                workflow_id=wid,
                document_id=did,
                requested_locales=["es-ES"],
                status="failed",
                finished_at=datetime.utcnow(),
            )
            session.add(batch)
            session.commit()
            session.refresh(batch)
            bid = batch.id
            session.add(
                WorkflowRun(
                    workflow_id=wid,
                    document_id=did,
                    batch_id=bid,
                    target_locale="es-ES",
                    status="failed",
                )
            )
            session.commit()
        # Keep dispatch stationary; launch serialization AND refresh use real DB rows.
        with patch_pieces(
            "app.services.batch_runs", "rerun_batch", side_effect=lambda s, **kw: kw["batch"]
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
            ) as client:
                response = await client.post(
                    f"/api/batches/{bid}/{mode}", json={"wait": True, "locales": ["es-ES"]}
                )
        assert response.status_code == 200, response.text
        out = response.json()
        assert out["waited"] is False, out
        assert out["requested_steps"] and out["members"][0]["step_runs"], out


def test_round6_identity_request_shapes():
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.services import apikeys
    from app.web import deps

    token = stress._api_key()
    for request, expected_calls in (
        (SimpleNamespace(headers={"X-Loc-Token": token}), 2),
        (SimpleNamespace(headers={"X-Loc-Token": token}, state=None), 2),
        (Request({"type": "http", "headers": [(b"x-loc-token", token.encode())]}), 1),
    ):
        with patch.object(apikeys, "verify_key", wraps=apikeys.verify_key) as verify:
            first = deps.api_identity(request)
            second = deps.api_identity(request)
        assert first is not None and first["kind"] == "user", first
        assert second is not None and second["actor"] == first["actor"], second
        assert verify.call_count == expected_calls, verify.call_count
        if expected_calls == 1:
            assert second is first
        else:
            assert getattr(request, "state", None) is None


def test_round5_auth_and_resume_refresh():
    import httpx
    from sqlalchemy import event
    from sqlmodel import Session
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from app.models import WorkflowRun
    from app.web import api_cli, streaming

    headers = {"X-Loc-Token": stress._api_key()}
    wid, did = stress._mk_wf("mock"), stress._mk_doc("es-ES", 2)
    with Session(db.engine) as session:
        run = WorkflowRun(workflow_id=wid, document_id=did, status="completed")
        session.add(run)
        session.commit()
        session.refresh(run)
        rid = run.id
    writes = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE APIKEY SET LAST_USED"):
            writes.append(statement)

    async def exercise():
        limiter = streaming.stream_limiter()
        borrower = object()
        limiter.acquire_on_behalf_of_nowait(borrower)
        try:
            with patch_api_cli("_gate", wraps=api_cli._gate) as gate:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
                ) as client:
                    response = await client.post(
                        f"/api/workflows/{wid}/run",
                        json={"wait": True, "doc_id": did, "locale": "es-ES"},
                    )
                assert gate.call_count == 1, f"gate calls: {gate.call_count}"
            assert response.status_code == 200 and response.json()["waited"] is False, response.text
            assert len(writes) == 1, f"last_used writes: {len(writes)}"

            async def handler(request):
                return JSONResponse({"run_id": rid, "status": "running"})

            request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
            request._body = b'{"wait":true}'
            with (
                patch_api_cli("_gate", return_value=({}, None, None)),
                patch_api_cli("_write_block", return_value=None),
            ):
                response = await api_cli._stream_wait_route(handler)(request=request)
            assert json.loads(response.body)["status"] == "completed", response.body
        finally:
            limiter.release_on_behalf_of(borrower)

    event.listen(db.engine, "before_cursor_execute", observe)
    try:
        with patch.object(config, "STREAM_WORKERS", 1), patch.object(config, "STREAM_WAIT_S", 0.01):
            asyncio.run(exercise())
    finally:
        event.remove(db.engine, "before_cursor_execute", observe)
    assert stress._wait_slots_idle() == 0


def test_round4_low_ratchets():
    import ast

    server = ast.parse(Path("integrations/mcp-server/server.py").read_text(encoding="utf-8"))
    workflow = next(node for node in server.body if getattr(node, "name", "") == "run_workflow")
    assert not any(
        isinstance(node, ast.keyword)
        and node.arg == "timeout"
        and isinstance(node.value, ast.Name)
        and node.value.id == "_SLOW_TIMEOUT"
        for node in ast.walk(workflow)
    ), "dead workflow launch timeout"
    source = Path("app/web/streaming.py").read_text(encoding="utf-8")
    functions = {
        node.name: node for node in ast.parse(source).body if isinstance(node, ast.AsyncFunctionDef)
    }
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "CapacityLimiter"
        for node in ast.walk(functions["run_sync"])
    ), "private no-op limiter returned"
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "getsource" for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and any(
            isinstance(arg, ast.Constant) and arg.value == "STREAM_WAIT_S" for arg in node.args
        ):
            assert not any(keyword.arg == "create" for keyword in node.keywords)
    import tempfile

    with tempfile.TemporaryDirectory(prefix="memory_inventory_") as directory:
        probe = Path(directory) / "_memory_inventory_probe.py"
        for source in (
            "from anyio.to_thread import run_sync as offload",
            "loop.run_in_executor(None, work)",
            "import asyncio as aio\naio.to_thread(work)",
        ):
            probe.write_text(source, encoding="utf-8")
            try:
                test_round3_inventory(roots=(Path(directory),))
            except AssertionError as exc:
                assert probe.name in str(exc), exc
            else:
                raise AssertionError(f"inventory accepted {source}")


def test_round3_guard_precedence():
    from app.services import orchestrator

    # #155 retired the FULL_TEXT x tm_match guard that used to sit between these two.
    guards = ["_refuse_archived", "_refuse_project_budget"]
    from contextlib import ExitStack

    with patch.object(config, "MAX_CONCURRENT_RUNS", 1), orchestrator._execution_slot():
        for arm in (orchestrator.run_workflow, orchestrator.start_run):
            for index, guard in enumerate(guards):
                calls = []

                def refuse(*args, **kwargs):
                    raise ValueError("guard wins")

                with ExitStack() as stack:
                    for name in guards:
                        stack.enter_context(
                            patch_pieces(
                                "app.services.orchestrator",
                                name,
                                side_effect=refuse
                                if name == guard
                                else lambda *a, calls=calls, **kw: calls.append(True),
                            )
                        )
                    try:
                        arm(Mock(), 1, 1, "es-ES")
                    except ValueError as exc:
                        assert str(exc) == "guard wins"
                    else:
                        raise AssertionError("guard was skipped")
                    assert len(calls) == index


def test_round3_low_contracts():
    import ast
    import builtins

    from app.cli import __main__ as cli

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    handlers = [
        node
        for node in ast.walk(functions["test_unknown_selection"])
        if isinstance(node, ast.ExceptHandler)
    ]
    assert all(
        isinstance(node.type, ast.Name) and node.type.id == "ValueError" for node in handlers
    )
    loops = [
        node for node in ast.walk(functions["test_stream_isolation"]) if isinstance(node, ast.While)
    ]
    assert any(
        isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Call)
        and isinstance(node.test.left.func, ast.Name)
        and node.test.left.func.id == "len"
        and any(
            isinstance(value, ast.Constant) and value.value == 2 for value in node.test.comparators
        )
        for node in loops
    )
    original_import = builtins.__import__

    def guard(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app" and "db" in fromlist:
            raise ImportError("broken db install")
        return original_import(name, globals, locals, fromlist, level)

    from app.services.orchestrator import RunCapacityError
    from tests._cli_runner import run_cli

    with (
        patch.object(cli, "_feature_dispatch_gate", side_effect=RunCapacityError(1, 1)),
        patch("builtins.__import__", guard),
    ):
        code, out, err = run_cli(["--json", "config", "list"])
        assert code == 1 and json.loads(out)["retry_after"] > 0, (out, err)
    for attributes in ({}, {"retry_after": 15}):
        unrelated = type("RunCapacityError", (Exception,), attributes)("original")
        with patch.object(cli, "_feature_dispatch_gate", side_effect=unrelated):
            try:
                cli.main(["--json", "config", "list"])
            except Exception as exc:
                assert exc is unrelated
            else:
                raise AssertionError("unrelated exception misclassified")


def test_round3_inventory(roots=None):
    import ast

    for root in roots if roots is not None else (Path("app"), Path("integrations")):
        for path in root.rglob("*.py"):
            if path.as_posix() == "app/web/streaming.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            asyncio_names = {"asyncio"}
            thread_names = {"to_thread"}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    asyncio_names.update(
                        n.asname or n.name for n in node.names if n.name == "asyncio"
                    )
                    thread_names.update(
                        n.asname or n.name for n in node.names if n.name == "anyio.to_thread"
                    )
                if isinstance(node, ast.ImportFrom) and node.module == "anyio":
                    thread_names.update(
                        n.asname or n.name for n in node.names if n.name == "to_thread"
                    )
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    n.name in {"StreamingResponse", "EventSourceResponse"} for n in node.names
                ):
                    assert node.module == "app.web.streaming", str(path)
                if isinstance(node, ast.Attribute):
                    assert node.attr not in {"StreamingResponse", "EventSourceResponse"}, str(path)
                    assert node.attr != "run_in_executor", f"unreviewed executor offload: {path}"
                    assert not (
                        node.attr == "to_thread"
                        and isinstance(node.value, ast.Name)
                        and node.value.id in asyncio_names
                    ), str(path)
                    if node.attr == "run_sync":
                        assert not (
                            (isinstance(node.value, ast.Name) and node.value.id in thread_names)
                            or (
                                isinstance(node.value, ast.Attribute)
                                and node.value.attr == "to_thread"
                            )
                        ), f"unreviewed to_thread offload: {path}"
                if isinstance(node, ast.ImportFrom) and any(
                    n.name == "run_in_threadpool" for n in node.names
                ):
                    raise AssertionError(f"unreviewed default offload: {path}")
                if isinstance(node, ast.ImportFrom):
                    assert not (
                        node.module == "anyio.to_thread"
                        and any(n.name == "run_sync" for n in node.names)
                    ), str(path)
                    assert not (
                        node.module == "asyncio" and any(n.name == "to_thread" for n in node.names)
                    ), str(path)
    # 2026-09-14 SRP split: the routes live in sibling pieces of app/web/api_cli.py
    routes = {}
    for piece in sorted(Path("app/web").glob("api_cli*.py")):
        source = piece.read_text(encoding="utf-8")
        routes.update(
            {
                node.name: node
                for node in ast.parse(source).body
                if isinstance(node, ast.AsyncFunctionDef)
            }
        )
    for name in (
        "api_workflows_run",
        "api_workflows_run_batch",
        "api_batches_rerun",
        "api_batches_retry_failed",
        "api_runs_resume",
    ):
        assert "waited:false" in ast.get_docstring(routes[name]), name
        assert any(
            isinstance(d, ast.Name) and d.id == "_stream_wait_route"
            for d in routes[name].decorator_list
        ), name


def _rss_mb():
    try:
        import psutil
    except ImportError:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t)
                    for name in (
                        "peak",
                        "working",
                        "paged_peak",
                        "paged",
                        "nonpaged_peak",
                        "nonpaged",
                        "pagefile",
                        "pagefile_peak",
                    )
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            get_info = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
            get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            assert get_info(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
            return counters.working / 1048576
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
            1048576 if sys.platform == "darwin" else 1024
        )
    return psutil.Process().memory_info().rss / 1048576


def test_mixed_load_and_recovery():
    from sqlmodel import Session

    from app.models import Project, RunStatus
    from app.services import keys, orchestrator, providers, settings
    from app.web import health
    from tests._cli_runner import run_cli

    assert stress._wait_slots_idle() == 0
    cap = config.MAX_CONCURRENT_RUNS
    assert cap > 0
    settings.set_many({"mt_concurrency": "4"})
    providers.register_provider("barriermock", stress._Barrier())
    plan = [(stress._mk_wf("barriermock"), stress._mk_doc("es-ES", 16)) for _ in range(cap + 4)]
    project_id = stress._key_project(["de-DE", "fr-FR", "ru-RU"], n_keys=16)
    fan_wid = stress._mk_wf("mock")
    headers = {"X-Loc-Token": stress._api_key()}
    stress._Barrier.release.clear()
    samples = []
    stop = threading.Event()

    def sample():
        while not stop.wait(0.01):
            samples.append(_rss_mb())

    sampler = threading.Thread(target=sample)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            client.get("/ready")
            baseline = _rss_mb()
            sampler.start()

            def launch(pair):
                wid, did = pair
                return client.post(
                    f"/api/workflows/{wid}/run",
                    json={"doc_id": did, "locale": "es-ES"},
                    headers=headers,
                )

            with ThreadPoolExecutor(max_workers=cap + 4) as executor:
                responses = list(executor.map(launch, plan))
            assert all(r.status_code in (200, 503) for r in responses)
            admitted = [r.json()["run_id"] for r in responses if r.status_code == 200]
            refused = [r for r in responses if r.status_code == 503]
            assert len(admitted) == cap and len(refused) == 4
            assert orchestrator.RUN_SLOTS.in_flight == cap
            expected = str(orchestrator.RunCapacityError(cap, cap))
            for response in refused:
                assert response.json()["reason"] == "run_capacity"
                assert response.json()["error"] == expected

            # Same real refusal relayed by the remote CLI and MCP adapters.
            from app.cli._common import CliError
            from app.cli._remote import RemoteClient

            try:
                RemoteClient.__new__(RemoteClient)._decode(refused[0])
                raise AssertionError("remote CLI admitted a refused launch")
            except CliError as exc:
                assert expected in str(exc)
                assert exc.payload["code"] == "run_capacity"
            path = Path("integrations/mcp-server/server.py")
            spec = importlib.util.spec_from_file_location("memory_mcp", path)
            relay = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(relay)
            assert expected == relay._explain_status(refused[0])

            wid, did = next(
                pair
                for pair, response in zip(plan, responses, strict=True)
                if response.status_code == 503
            )
            web = client.post(
                f"/documents/{did}/run",
                data={"workflow_id": wid, "locale": "es-ES"},
                follow_redirects=False,
            )
            assert web.status_code == 503, (web.status_code, dict(web.headers), web.text[:200])
            assert expected in web.text
            code, out, err = run_cli(["--json", "workflow", "run", str(did), str(wid), "es-ES"])
            assert code != 0, (code, out, err)
            assert json.loads(out)["error"] == expected
            assert json.loads(out)["reason"] == "run_capacity"
            assert json.loads(out)["status"] == 503
            import httpx

            from app.cli import _remote

            def forward(request):
                response = client.request(
                    request.method,
                    request.url.path,
                    content=request.content,
                    headers=dict(request.headers),
                )
                return httpx.Response(
                    response.status_code,
                    content=response.content,
                    headers=dict(response.headers),
                    request=request,
                )

            with patch.object(_remote, "_TRANSPORT", httpx.MockTransport(forward)):
                remote_code, remote_out, remote_err = run_cli(
                    [
                        "--json",
                        "--remote",
                        "http://127.0.0.1",
                        "--token",
                        headers["X-Loc-Token"],
                        "workflow",
                        "run",
                        str(did),
                        str(wid),
                        "es-ES",
                    ]
                )
            assert remote_code != 0, (remote_out, remote_err)
            assert json.loads(remote_out)["error"] == expected, (remote_out, remote_err)
            with Session(db.engine) as session:
                fan = keys.launch_translation(
                    session, session.get(Project, project_id), fan_wid, "all", actor="fixture"
                )
            queued = [row["run_id"] for row in fan["queued"]]
            assert len(queued) == 3 and not fan["launched"], fan
            held_rss = _rss_mb()
            stress._Barrier.release.set()
            outcomes = stress._wait_terminal(admitted + queued)
            assert all(row[0] == RunStatus.COMPLETED for row in outcomes.values()), outcomes
            assert stress._wait_slots_idle() == 0

            # Saturation is healthy by D8. A failed DB probe recovers without a restart.
            with patch.object(health, "db_ready", return_value=(False, "probe_test")):
                assert client.get("/ready").status_code == 503
            held = [db.engine.raw_connection() for _ in range(stress._pool_capacity())]
            try:
                assert client.get("/ready").status_code == 200
            finally:
                for connection in held:
                    connection.close()
            deadline = time.monotonic() + 5
            while db.engine.pool.checkedout() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert db.engine.pool.checkedout() == 0
            assert db.engine.pool.checkedin() == config.DB_POOL_SIZE
            assert client.get("/ready").status_code == 200
            assert _rss_mb() - baseline < 256
            measurements = {
                "baseline_mb": baseline,
                "held_mb": held_rss,
                "per_run_delta_mb": max(0, held_rss - baseline) / cap,
                "peak_mb": max([*samples, _rss_mb()]),
                "runs": cap,
                "retained_connections": db.engine.pool.checkedin(),
            }
            assert measurements["peak_mb"] - baseline < 256, measurements
            print(json.dumps(measurements))
            if os.environ.get("MEMORY_REPORT_PATH"):
                Path(os.environ["MEMORY_REPORT_PATH"]).write_text(
                    json.dumps(measurements), encoding="utf-8"
                )
    finally:
        stress._Barrier.release.set()
        stop.set()
        if sampler.ident:
            sampler.join(5)
        stress._wait_slots_idle()


def test_later_watchdog_backpressure():
    import queue

    from app.services import providers

    queues, pumps = [], []
    real_queue, real_thread = queue.Queue, threading.Thread

    def tracked_queue(*args, **kwargs):
        q = real_queue(*args, **kwargs)
        queues.append(q)
        return q

    def tracked_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        pumps.append(thread)
        return thread

    closed = threading.Event()

    def chunks():
        try:
            yield "first"
            yield from range(100)
        finally:
            closed.set()

    with (
        patch.object(config, "RUN_WORKERS", 2),
        patch.object(config, "PROVIDER_TIMEOUT_S", 0.05),
        patch.object(queue, "Queue", tracked_queue),
        patch.object(threading, "Thread", tracked_thread),
    ):
        stream = providers._stall_watchdog(chunks())
        try:
            assert next(stream) == "first"
            pumps[0].join(1)
            assert queues[0].maxsize == 2 and queues[0].qsize() <= 2
            assert not pumps[0].is_alive(), "stalled consumer retained pump"
            assert closed.is_set(), "source iterator was not closed"
            try:
                list(stream)
            except providers.ProviderStalled as exc:
                assert "consumer" in str(exc)
            else:
                raise AssertionError("overflow was silently reported as success")
        finally:
            stream.close()
    assert list(providers._stall_watchdog(iter([1, 2, 3]))) == [1, 2, 3]


def test_later_watchdog_capacity():
    from app.services import providers

    entered, release = threading.Event(), threading.Event()
    slots = threading.BoundedSemaphore(1)

    def blocked():
        entered.set()
        release.wait(3)
        yield "late"

    with (
        patch_pieces("app.services.providers", "_PUMP_SLOTS", slots),
        patch.object(config, "PROVIDER_TIMEOUT_S", 0.05),
    ):
        stream = providers._stall_watchdog(blocked())
        try:
            try:
                next(stream)
            except providers.ProviderStalled:
                pass
            else:
                raise AssertionError("upstream stall did not fail")
            assert entered.is_set()
            try:
                next(providers._stall_watchdog(iter(["excess"])))
            except providers.ProviderStalled as exc:
                assert "capacity" in str(exc)
            else:
                raise AssertionError("abandoned pump released its live reservation")
        finally:
            release.set()
            stream.close()
        assert slots.acquire(timeout=2), "pump reservation leaked"
        slots.release()
        assert list(providers._stall_watchdog(iter(["recovered"]))) == ["recovered"]
    with patch.object(config, "STREAM_WORKERS", config.STREAM_WORKERS + 1):
        higher = config.memory_estimate_mb()
    # One stream thread, one pump, W children, one parent working allowance.
    assert higher - config.memory_estimate_mb() == 16 + config.RUN_WORKERS * 8 + 32


def test_later_cli_admitted_wait():
    import httpx

    from app.cli import _remote
    from app.cli._remote import RemoteClient
    from app.services import orchestrator, providers
    from app.web import api_cli

    providers.register_provider("barriermock", stress._Barrier())
    wid, did = stress._mk_wf("barriermock"), stress._mk_doc("es-ES", 2)
    token = stress._api_key()
    stress._Barrier.release.clear()
    real_drain = api_cli._drain_run_to_terminal
    drains = []
    held = []

    def drain(run_id):
        drains.append(run_id)
        assert orchestrator.RUN_SLOTS.in_flight == config.MAX_CONCURRENT_RUNS
        assert not stress._Barrier.release.is_set()
        stress._Barrier.release.set()
        return real_drain(run_id)

    with TestClient(app) as client:

        def forward(request):
            assert json.loads(request.content)["wait"] is True
            response = client.request(
                request.method,
                request.url.path,
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers=dict(response.headers),
                request=request,
            )

        try:
            for _ in range(config.MAX_CONCURRENT_RUNS - 1):
                slot, _ = orchestrator.RUN_SLOTS.acquire()
                assert slot is not None
                held.append(slot)
            assert client.get("/health").status_code == 200
            with (
                patch.object(_remote, "_TRANSPORT", httpx.MockTransport(forward)),
                patch_api_cli("_drain_run_to_terminal", drain),
            ):
                result = RemoteClient("http://127.0.0.1", token).post(
                    f"/api/workflows/{wid}/run",
                    json={"doc_id": did, "locale": "es-ES", "wait": True},
                )
            assert len(drains) == 1, "CLI bypassed admitted stream wait"
            assert result["status"].lower() == "completed", result
        finally:
            stress._Barrier.release.set()
            for slot in held:
                slot.release()
            assert stress._wait_slots_idle() == 0


def test_later_mcp_terminal_noop():
    import httpx

    spec = importlib.util.spec_from_file_location(
        "memory_later_mcp", "integrations/mcp-server/server.py"
    )
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)

    async def exercise():
        for launch_status, expected_polls in (("completed", 0), ("running", 1)):
            requests = []

            def respond(request, requests=requests, launch_status=launch_status):
                requests.append(request.method)
                return httpx.Response(200, json={"id": 17, "status": launch_status})

            def client():
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(respond), base_url="http://test"
                )

            from unittest.mock import AsyncMock

            status = AsyncMock(return_value=json.dumps({"id": 17, "status": "completed"}))
            with (
                patch.object(relay, "_client", client),
                patch.object(relay, "TOKEN", "offline"),
                patch.object(relay, "work_status", status),
            ):
                result = json.loads(
                    await relay.run_workflow("retry_failed", batch_id=17, confirm=True)
                )
            assert status.call_count == expected_polls, (launch_status, status.call_count)
            assert requests == ["POST"] and result["done"] is True, result

    asyncio.run(exercise())


def test_later_maintenance_overlap():
    from app.services import lifecycle, retention, scheduler

    for service, entry, target, name, busy in (
        (scheduler, scheduler.tick, lifecycle, "auto_close_tick", None),
        (retention, retention.purge, retention, "enabled", {"busy": True}),
    ):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def hold(*args, calls=calls, entered=entered, release=release, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                assert release.wait(3)
            return False

        with patch.object(target, name, hold):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(entry)
                try:
                    assert entered.wait(2)
                    assert entry() == busy, service.__name__
                    assert calls == [1], "overlapping maintenance entered its body"
                    if service is scheduler:
                        assert scheduler.tick_now() == (False, None)
                finally:
                    release.set()
                    pending.result(timeout=5)
            assert entry() != busy, "completed pass did not release admission"


def test_later_mcp_missing_refusal():
    stress.test_round7_missing_batch_boundaries()


def test_later_memory_profiles():
    for r, pool, overflow, cache, a, w, expected in (
        (8, 20, 40, 1000, 16, 4, 5002.59375),
        (4, 20, 20, 1024, 8, 2, 3320),
        (8, 20, 40, 1000, 16, 4, 5002.59375),
        (16, 64, 64, 1024, 24, 4, 6288),
    ):
        with patch.multiple(
            config,
            MAX_CONCURRENT_RUNS=r,
            DB_POOL_SIZE=pool,
            DB_MAX_OVERFLOW=overflow,
            SQLITE_CACHE_KB=cache,
            ANYIO_WORKERS=a,
            RUN_WORKERS=w,
            STREAM_WORKERS=32,
            JOB_WORKERS=2,
        ):
            assert config.memory_estimate_mb() == expected, config.memory_estimate_mb()
        for path in ("docs/architecture/configuration.md", "docs/DEPLOY.md"):
            assert f"{expected:g}" in Path(path).read_text(encoding="utf-8") or (
                f"{expected:.1f}" in Path(path).read_text(encoding="utf-8")
            ), path


def _later_refuses_then_enters(calls):
    """Invalid inputs must reach the body only after a reservation is available."""
    from app.services import orchestrator

    with patch.object(config, "MAX_CONCURRENT_RUNS", 1):
        for call, body_error in calls:
            token, _ = orchestrator.RUN_SLOTS.acquire()
            assert token is not None
            try:
                try:
                    call()
                except orchestrator.RunCapacityError as exc:
                    assert exc.in_flight == exc.cap == 1
                else:
                    raise AssertionError("saturated direct work was accepted")
            finally:
                token.release()
            try:
                result = call()
            except Exception as exc:
                assert not isinstance(exc, orchestrator.RunCapacityError), exc
                assert body_error is not None and isinstance(exc, body_error), exc
            else:
                assert (body_error is None and result is None) or (
                    not isinstance(body_error, type) and body_error(result)
                ), (call, result)
            assert orchestrator.RUN_SLOTS.in_flight == 0


def test_later_direct_jobs_admission():
    from app.services import data_export, export, jobs, reports

    _later_refuses_then_enters(
        [
            (lambda: jobs.run_sync("missing-later-handler", {}), ValueError),
            (lambda: jobs._execute(-1), None),
            (lambda: data_export._execute_job({}), KeyError),
            (lambda: data_export.run_export(user=None), lambda row: row.status == "ok"),
            (lambda: reports.run_report(None, None), AttributeError),
            (lambda: reports.to_csv({}), KeyError),
            (lambda: export.build_translated_file(None, None, "de"), AttributeError),
        ]
    )


def test_later_consumer_allowance():
    from sqlmodel import Session

    from app.models import Job
    from app.ports import jobqueue
    from app.services import data_export, export, jobs, orchestrator, reports

    calls = [
        ("job", lambda: jobs.run_sync("later_leaf", {}), lambda row: row.status == "completed"),
        ("execute", lambda: jobs._execute(-1), lambda result: result is None),
        ("data_export", lambda: data_export.run_export(user=None), lambda row: row.status == "ok"),
        ("execute_job", lambda: data_export._execute_job({}), KeyError),
        ("report", lambda: reports.run_report(None, None), AttributeError),
        (
            "csv",
            lambda: reports.to_csv({"columns": ["group"], "rows": [], "key_column": "group"}),
            lambda result: result == "group\r\nTOTALS\r\n",
        ),
        ("translated", lambda: export.build_translated_file(None, None, "de"), AttributeError),
    ]
    with (
        patch.object(config, "MAX_CONCURRENT_RUNS", 1),
        patch.dict(jobs.HANDLERS, later_leaf=lambda payload, progress: {"leaf": True}),
    ):
        token, _ = orchestrator.RUN_SLOTS.acquire()
        assert token is not None
        try:
            for family, invoke, expected in calls:

                def handler(payload, progress, invoke=invoke, expected=expected, family=family):
                    assert orchestrator.RUN_SLOTS.in_flight == 1
                    try:
                        result = invoke()
                    except Exception as exc:
                        assert isinstance(expected, type) and isinstance(exc, expected), exc
                        assert not isinstance(exc, orchestrator.RunCapacityError)
                    else:
                        assert not isinstance(expected, type) and expected(result), result
                    return {"entered": family}

                with Session(db.engine) as session:
                    job = Job(kind="later_parent", payload={})
                    session.add(job)
                    session.commit()
                    session.refresh(job)
                    jid = job.id
                queue = Mock()
                queue.claim.side_effect = [jid, StopIteration]
                with (
                    patch.object(jobqueue, "get", return_value=queue),
                    patch.dict(jobs.HANDLERS, later_parent=handler),
                    suppress(StopIteration),
                ):
                    jobs._worker_loop()
                row = jobs.get(jid)
                assert row.status == "completed" and row.result == {"entered": family}, (
                    family,
                    row.error,
                )
                queue.release.assert_called_once_with(jid)
                assert orchestrator.RUN_SLOTS.in_flight == 1
                # A reused worker thread must lose its prepaid context after release.
                try:
                    reports.to_csv({})
                except orchestrator.RunCapacityError:
                    pass
                else:
                    raise AssertionError("consumer allowance leaked into direct work")
        finally:
            token.release()


def main():
    checks = [
        test_later_consumer_allowance,
        test_later_direct_jobs_admission,
        test_later_watchdog_backpressure,
        test_later_watchdog_capacity,
        test_later_cli_admitted_wait,
        test_later_mcp_terminal_noop,
        test_later_maintenance_overlap,
        test_later_mcp_missing_refusal,
        test_later_memory_profiles,
        test_budget_boot,
        test_job_workers_and_config_list,
        test_worker_limiter,
        test_child_workers,
        test_sync_admission,
        test_reserved_batch_and_resume,
        test_sqlite_mmap,
        test_mixed_load_and_recovery,
        test_stream_isolation,
        test_slot_release_wakes,
        test_abandoned_stream,
        test_sync_summary_ownership,
        test_unknown_selection,
        test_cli_import_failure,
        test_mcp_structured_refusal,
        test_sqlite_connection_closed,
        test_capacity_masking,
        test_capacity_docs,
        test_round3_wait_capacity,
        test_round3_stream_capacity,
        test_round3_cleanup,
        test_round3_guard_precedence,
        test_round3_low_contracts,
        test_round3_inventory,
        test_round4_mcp_poll,
        test_round4_wait_guards_and_headers,
        test_round4_batch_poll,
        test_round4_low_ratchets,
        test_round5_auth_and_resume_refresh,
        test_round6_identity_request_shapes,
    ]
    selected = os.environ.get("MEMORY_CHECK")
    assert {f.__name__ for f in checks} == {name for name in globals() if name.startswith("test_")}
    assert len(checks) == len(set(checks))
    if selected is not None and selected not in {f.__name__ for f in checks}:
        known = ", ".join(f.__name__ for f in checks)
        raise ValueError(f"Unknown MEMORY_CHECK {selected!r}; known checks: {known}")
    count = 0
    for check in checks:
        if not selected or selected == check.__name__:
            check()
            count += 1
    print(f"verify_memory_bound ({count} checks) OK")


if __name__ == "__main__":
    main()
