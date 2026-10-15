"""Executed #109 review regressions. Invoke only through test_verify_suites."""

import ast
import asyncio
import importlib.util
import json
import os
import queue
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_later_review_")

from app import config
from app.services import providers
from tests import verify_memory_bound as memory


def _mock_transport_for(token_client):
    """httpx.MockTransport forwarding a client to the in-process TestClient (no network)."""
    import httpx

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = token_client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    return httpx.MockTransport(handler)


def test_setup_release():
    for allocation in ("Queue", "Event"):
        slots = threading.BoundedSemaphore(1)
        owner = queue if allocation == "Queue" else threading
        with (
            patch_pieces("app.services.providers", "_PUMP_SLOTS", slots),
            patch.object(owner, allocation, side_effect=MemoryError("allocation fixture")),
        ):
            try:
                next(providers._stall_watchdog(iter([1])))
            except MemoryError:
                pass
            else:
                raise AssertionError("allocation failure was hidden")
            assert slots.acquire(blocking=False), f"{allocation} allocation leaked pump slot"
            slots.release()


def test_consumer_wait():
    calls = []
    original = queue.Queue.get
    release = threading.Event()

    def get(self, *args, **kwargs):
        calls.append(kwargs.get("timeout"))
        return original(self, *args, **kwargs)

    def source():
        release.wait(0.3)
        yield "delivered"

    with patch.object(queue.Queue, "get", get), patch.object(config, "PROVIDER_TIMEOUT_S", 1):
        assert list(providers._stall_watchdog(source())) == ["delivered"]
    assert len(calls) <= 4, f"consumer polled {len(calls)} times during 0.3s silence"


def test_abandoned_put():
    full = threading.Event()
    closed = threading.Event()
    original = queue.Queue.put
    slots = threading.BoundedSemaphore(1)

    def put(self, *args, **kwargs):
        if self.full() and args[0][1] >= 2:
            full.set()
        return original(self, *args, **kwargs)

    def source():
        try:
            yield from range(100)
        finally:
            closed.set()

    with (
        patch.object(queue.Queue, "put", put),
        patch.object(config, "RUN_WORKERS", 1),
        patch.object(config, "PROVIDER_TIMEOUT_S", 2),
        patch_pieces("app.services.providers", "_PUMP_SLOTS", slots),
    ):
        stream = providers._stall_watchdog(source())
        assert next(stream) == 0
        assert full.wait(1)
        stream.close()
        try:
            assert closed.wait(0.5), "abandoned full-queue pump retained its reservation"
            assert slots.acquire(timeout=0.5)
            slots.release()
        finally:
            assert closed.wait(3)


def test_close_failure():
    class Source:
        def __iter__(self):
            return iter([1, 2])

        def close(self):
            raise OSError("cleanup fixture")

    with patch.object(providers.log, "warning") as warning:
        assert list(providers._stall_watchdog(Source())) == [1, 2]
    warning.assert_called_once_with("Provider stream close failed (%s)", "OSError")


def test_direct_release_wakes_queue():
    from app.services import maintenance, orchestrator

    slots = orchestrator._RunSlots()
    ready, admitted = threading.Event(), threading.Event()
    wake = threading.Event()

    def waiter():
        ready.set()
        if wake.wait(orchestrator.QUEUE_POLL_S):
            token = slots.acquire_for_queue()
            if token:
                admitted.set()
                token.release()

    @maintenance.admitted_work
    def direct():
        thread.start()
        assert ready.wait(1)

    thread = threading.Thread(target=waiter)
    with (
        patch.object(config, "MAX_CONCURRENT_RUNS", 1),
        patch_pieces("app.services.orchestrator", "RUN_SLOTS", slots),
        patch.object(orchestrator.ADMISSION, "_wake", wake),
    ):
        try:
            start = time.monotonic()
            direct()
            assert admitted.wait(0.5), "direct release did not wake queued admission within 0.5s"
            assert time.monotonic() - start < orchestrator.QUEUE_POLL_S / 2
        finally:
            wake.set()
            thread.join(1)
    assert not thread.is_alive()
    assert slots.in_flight == 0


def test_direct_refusal_fairness():
    from app.services import maintenance, orchestrator

    # Execute scratch copies of the current admission seam, never mutate live modules.
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for module in (maintenance, orchestrator):
            (root / Path(module.__file__).name).write_bytes(Path(module.__file__).read_bytes())
        tree = ast.parse((root / "orchestrator.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_RunSlots")
        namespace = dict(vars(orchestrator))
        exec(
            compile(ast.Module(body=[cls], type_ignores=[]), str(root / "orchestrator.py"), "exec"),
            namespace,
        )
        seam = {"__name__": "scratch_maintenance"}
        exec(
            compile(
                (root / "maintenance.py").read_text(encoding="utf-8"),
                str(root / "maintenance.py"),
                "exec",
            ),
            seam,
        )
        slots = namespace["_RunSlots"]()
        with (
            patch.object(config, "MAX_CONCURRENT_RUNS", 1),
            patch_pieces("app.services.orchestrator", "RUN_SLOTS", slots),
        ):
            held, _ = slots.acquire()
            try:
                seam["admitted_work"](lambda: None)()
            except orchestrator.RunCapacityError:
                pass
            else:
                raise AssertionError("direct work exceeded cap")
            refused = slots._refused_since_yield
            held.release()
            queued = slots.acquire_for_queue()
            print(
                f"scratch direct refusal: counter={refused}; queued_admitted={queued is not None}; "
                f"in_flight={slots.in_flight}"
            )
            if queued:
                queued.release()
            assert queued is not None, (
                "direct refusal armed launch fairness and delayed queued admission"
            )
            held, _ = slots.acquire()
            assert slots.acquire()[0] is None
            held.release()
            assert slots.acquire_for_queue() is None, "interactive launch lost its fairness window"


def test_capacity_operation_messages():
    from starlette.requests import Request

    from app import main as web
    from app.services import export, orchestrator, reports

    slots = orchestrator._RunSlots()
    failures = []
    with (
        patch.object(config, "MAX_CONCURRENT_RUNS", 1),
        patch_pieces("app.services.orchestrator", "RUN_SLOTS", slots),
    ):
        held, _ = slots.acquire()
        try:
            for fn, operation in (
                (reports.run_report, "report"),
                (reports.to_csv, "report"),
                (export.build_translated_file, "export"),
            ):
                try:
                    fn()
                except orchestrator.RunCapacityError as exc:
                    for path in ("/api/download", "/download"):
                        request = Request(
                            {"type": "http", "method": "GET", "path": path, "headers": []}
                        )
                        with patch.object(web._logger, "warning") as warning:
                            response = asyncio.run(web._run_capacity_error(request, exc))
                        logged = warning.call_args.args[0] % warning.call_args.args[1:]
                        body = response.body.decode()
                        assert response.status_code == 503
                        assert response.headers["Retry-After"] == str(exc.retry_after)
                        if path.startswith("/api"):
                            assert json.loads(body)["reason"] == "run_capacity"
                        if (
                            f"refused {'an' if operation == 'export' else 'a'} {operation}"
                            not in logged
                            or f"{operation} was refused" not in body
                            or "launch" in body
                        ):
                            failures.append(f"{fn.__name__} {path}: {logged}; {exc!s}")
                else:
                    raise AssertionError("saturated operation was admitted")
        finally:
            held.release()
    assert not failures, "\n".join(failures)


def _relay():
    spec = importlib.util.spec_from_file_location("review_mcp", "integrations/mcp-server/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retention_surfaces():
    import httpx

    from app.cli import _remote, cmd_admin
    from app.cli._common import CliError
    from app.ports.scheduler import ExternalTickDriver, InProcessTimeDriver
    from app.services import audit, retention, settings
    from tests._cli_runner import run_cli

    key = memory.stress._api_key(scopes="api docs admin")
    settings.set_many(
        {"remote_admin_enabled": "1", "retention_enabled": "1", "retention_days": "30"}
    )
    api = memory.TestClient(memory.app)
    transport = _mock_transport_for(api)
    relay = _relay()
    relay.TOKEN = key
    path = "/api/admin/retention/purge"
    message = "a retention purge is already running; try again shortly."
    counts = dict.fromkeys(("documents", "segments", "jobs", "tm", "files", "key_image_blobs"), 0)
    try:
        for busy in (True, False):
            result = {"busy": True} if busy else counts
            with (
                patch.object(retention, "purge", return_value=result),
                patch.object(audit, "log") as logged,
                patch.object(cmd_admin, "_retention_preview", return_value=counts),
            ):
                response = api.post(path, json={"confirm": "purge"}, headers={"X-Loc-Token": key})
                assert response.status_code == (409 if busy else 200), response.text
                if busy:
                    assert response.json() == {"ok": False, "busy": True, "error": message}
                    logged.assert_not_called()
                else:
                    assert response.json() == {"ok": True, "removed": counts}
                    assert (
                        sum(c.kwargs["action"] == "retention.purge" for c in logged.call_args_list)
                        == 1
                    )
                code, out, err = run_cli(["admin", "retention", "purge-now", "--yes"])
                assert (code != 0) == busy, (code, out, err)
                if busy:
                    assert message in err and "Traceback" not in err, err
                    assert len(err.strip().splitlines()) == 1, err
                    assert "Purge complete" not in out
                else:
                    assert "Purge complete: removed 0 document(s)" in out
                client = _remote.RemoteClient("http://127.0.0.1", key, transport=transport)
                try:
                    relayed = client.post(path, json={"confirm": "purge"})
                except CliError as exc:
                    assert busy and message in str(exc), str(exc)
                else:
                    assert not busy and relayed["removed"] == counts

                async def mcp():
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers={"X-Loc-Token": key},
                    ) as client:
                        return await relay._post_json(client, path, {"confirm": "purge"})

                try:
                    relayed = asyncio.run(mcp())
                except relay.ToolError as exc:
                    assert busy and message in str(exc), str(exc)
                else:
                    assert not busy and relayed["removed"] == counts
                for driver in (ExternalTickDriver(), InProcessTimeDriver()):
                    assert driver.sweep() == result
    finally:
        api.close()
        settings.set_many({"remote_admin_enabled": "0", "retention_enabled": "0"})


def test_retry_detail():
    from datetime import datetime

    import httpx
    from sqlmodel import Session

    from app import db
    from app.models import RunStatus, WorkflowBatch

    bid, _ = memory.stress._round6_batch(RunStatus.COMPLETED)
    with Session(db.engine) as session:
        batch = session.get(WorkflowBatch, bid)
        batch.status = "completed"
        batch.finished_at = datetime.now()
        session.add(batch)
        session.commit()
    key = memory.stress._api_key()
    headers = {"X-Loc-Token": key}
    with memory.TestClient(memory.app) as api:
        post = api.post(f"/api/batches/{bid}/retry-failed", json={"wait": False}, headers=headers)
        get = api.get(f"/api/batches/{bid}?detail=steps", headers=headers)
        assert post.status_code == get.status_code == 200, (post.text, get.text)
        launch, detail = post.json(), get.json()
        assert launch["retried"] == [] and launch["status"] == "completed"
        assert all(launch[k] == v for k, v in detail.items()), (launch, detail)
        relay = _relay()
        relay.TOKEN = key
        transport = _mock_transport_for(api)
        with (
            patch.object(
                relay,
                "_client",
                lambda: httpx.AsyncClient(
                    transport=transport, base_url="http://testserver", headers=headers
                ),
            ),
            patch.object(relay, "work_status", side_effect=AssertionError("redundant detail read")),
        ):
            result = json.loads(
                asyncio.run(relay.run_workflow(mode="retry_failed", batch_id=bid, confirm=True))
            )
        assert result["done"] is True
        assert all(result[k] == v for k, v in detail.items() if k != "ok")


def test_review_descriptors():
    import inspect

    from app.ports.scheduler import ExternalTickDriver, InProcessTimeDriver, TimeDriver
    from app.services import orchestrator, scheduler

    for obj in (TimeDriver, ExternalTickDriver.sweep, InProcessTimeDriver.sweep):
        assert '"busy": True' in inspect.getdoc(obj), obj
    assert "_tick_lock" not in inspect.getsource(InProcessTimeDriver.tick)
    assert "MaintenancePass" in inspect.getsource(scheduler._loop)
    with (
        patch.object(scheduler.time, "sleep", side_effect=[None, StopIteration]),
        patch.object(orchestrator.ADMISSION, "supervise"),
        patch.object(scheduler, "tick", return_value=None) as tick,
    ):
        with suppress(StopIteration):
            scheduler._loop()
        tick.assert_called_once_with()
    initial = providers._PUMP_SLOTS._initial_value
    with patch.object(config, "STREAM_WORKERS", initial + 1):
        assert providers._PUMP_SLOTS._initial_value == initial
    # SRP split 2026-09-15: the pump-slot note lives in a providers piece; read the whole family
    providers_family = "".join(
        inspect.getsource(m)
        for name, m in sorted(sys.modules.items())
        if (name == "app.services.providers" or name.startswith("app.services.providers_"))
        and m is not None
    )
    assert "restart to change this import-time ceiling" in providers_family


def main():
    checks = [
        test_setup_release,
        test_consumer_wait,
        test_abandoned_put,
        test_close_failure,
        test_direct_release_wakes_queue,
        test_direct_refusal_fairness,
        test_capacity_operation_messages,
        test_retention_surfaces,
        test_retry_detail,
        test_review_descriptors,
    ]
    selected = os.environ.get("REVIEW_CHECK")
    assert selected is None or selected in {f.__name__ for f in checks}
    for check in checks:
        if selected is None or selected == check.__name__:
            check()
    print("LATER PATHS REVIEW OK")


if __name__ == "__main__":
    main()
