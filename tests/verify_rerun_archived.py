"""#619: re-run and retry-failed refuse an archived workflow (or project), like the run path.

`rerun_batch` is the one choke point under the web routes, the /api routes (MCP proxies
them), and the CLI. Hermetic and offline (MockProvider). Covers: archive after a batch ran
-> service rerun, service retry-failed, web rerun, web retry-failed, /api rerun, /api
retry-failed, CLI rerun and CLI retry-failed are ALL refused with the SAME message the
normal run path (`_validate`) gives and NO new member run is created; an archived project
is refused too; unarchive -> re-run works again.

Final line: "RERUN ARCHIVED OK".
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_rerunarch_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    BatchStatus,
    Document,
    Project,
    RunStatus,
    Segment,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
)
from app.services import apikeys, batch_runs, security, settings
from app.services import workflows as W

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

_N = 0


def _uniq(prefix):
    global _N
    _N += 1
    return f"{prefix}-{_N}"


def _mk_failed_batch():
    """A finished batch with one FAILED member (es-ES): the exact shape re-run / retry-failed
    act on. Returns (project_id, doc_id, workflow_id, batch_id)."""
    with Session(engine) as s:
        p = Project(name=_uniq("RAProj"), source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="es-ES"))
        doc = Document(
            project_id=p.id,
            filename="ra.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hello world."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale="es-ES",
                order_index=0,
                source_text="Hello world.",
                target_text="",
            )
        )
        s.commit()
        w = W.create_workflow(s, _uniq("RAWf"))
        W.add_step(
            s,
            w.id,
            name="Translator",
            step_type="llm_agent",
            provider="mock",
            model="m1",
            prompt_text="Translate {text}",
            chunk_policy="none",
        )
        b = WorkflowBatch(
            workflow_id=w.id,
            document_id=doc.id,
            requested_locales=["es-ES"],
            status=BatchStatus.FAILED,
            finished_at=datetime.utcnow(),
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        s.add(
            WorkflowRun(
                workflow_id=w.id,
                document_id=doc.id,
                target_locale="es-ES",
                status=RunStatus.FAILED,
                batch_id=b.id,
                finished_at=datetime.utcnow(),
            )
        )
        s.commit()
        return p.id, doc.id, w.id, b.id


def _member_count(bid):
    with Session(engine) as s:
        return len(s.exec(select(WorkflowRun).where(WorkflowRun.batch_id == bid)).all())


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue() + err.getvalue()


def _refusal_text(wf_id):
    """The message the NORMAL run path gives for this archived workflow."""
    with Session(engine) as s:
        wf = s.get(Workflow, wf_id)
        return f"workflow {wf.id} ({wf.name}) is archived"


def _all_surfaces_refuse(bid, wf_id):
    want = _refusal_text(wf_id)
    before = _member_count(bid)
    # service: rerun and retry-failed (the failed set)
    for locales in (["es-ES"], batch_runs.failed_locales(Session(engine), _batch(bid))):
        with Session(engine) as s:
            try:
                batch_runs.rerun_batch(s, batch=_batch(bid, s), locales=locales, synchronous=True)
            except batch_runs.BatchError as exc:
                assert want in str(exc), str(exc)
            else:
                raise AssertionError("rerun_batch started work on an archived workflow")
    client = TestClient(app)
    r = client.post(f"/batches/{bid}/rerun", data={"locale": "es-ES"}, follow_redirects=False)
    assert r.status_code == 400 and want in r.text, (r.status_code, r.text[:300])
    r = client.post(f"/batches/{bid}/retry-failed", follow_redirects=False)
    assert r.status_code == 400 and want in r.text, (r.status_code, r.text[:300])
    admin = security.create_user(_uniq("ra") + "@x.io", "ra", "admin", "fixture-pass-1234!!")
    _row, raw = apikeys.create_key(admin.id, "ra-key", scopes="api docs")
    h = {"X-Loc-Token": raw}
    for wait in (False, True):
        r = client.post(
            f"/api/batches/{bid}/rerun", headers=h, json={"locale": "es-ES", "wait": wait}
        )
        assert r.status_code == 400 and want in r.json()["error"], (wait, r.status_code, r.text)
        r = client.post(f"/api/batches/{bid}/retry-failed", headers=h, json={"wait": wait})
        assert r.status_code == 400 and want in r.json()["error"], (wait, r.status_code, r.text)
    code, text = _run_cli(["workflow", "batch", "rerun", str(bid), "--locale", "es-ES"])
    assert code != 0 and want in text, (code, text)
    code, text = _run_cli(["workflow", "batch", "retry-failed", str(bid)])
    assert code != 0 and want in text, (code, text)
    assert _member_count(bid) == before, "a refused re-run must not create a member run"


def _batch(bid, s=None):
    if s is None:
        s = Session(engine)
    return s.get(WorkflowBatch, bid)


def test_archived_workflow_refused_then_unarchive_works():
    _pid, _did, wf, bid = _mk_failed_batch()
    with Session(engine) as s:
        W.archive_workflow(s, wf)
    _all_surfaces_refuse(bid, wf)
    with Session(engine) as s:
        W.unarchive_workflow(s, wf)
    before = _member_count(bid)
    with Session(engine) as s:
        batch_runs.rerun_batch(s, batch=_batch(bid, s), locales=["es-ES"], synchronous=True)
    assert _member_count(bid) == before + 1, "after unarchive the re-run must work again"
    print("  archived workflow: all surfaces refused; unarchive works OK")


def test_archived_project_refused():
    pid, _did, _wf, bid = _mk_failed_batch()
    with Session(engine) as s:
        p = s.get(Project, pid)
        p.archived_at = datetime.utcnow()
        s.add(p)
        s.commit()
    before = _member_count(bid)
    with Session(engine) as s:
        try:
            batch_runs.rerun_batch(s, batch=_batch(bid, s), locales=["es-ES"], synchronous=True)
        except batch_runs.BatchError as exc:
            assert "is archived" in str(exc), str(exc)
        else:
            raise AssertionError("rerun_batch started work in an archived project")
    assert _member_count(bid) == before
    print("  archived project: rerun refused OK")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failures += 1
                import traceback

                print(f"  FAIL {name}: {exc}")
                traceback.print_exc()
    if failures:
        print(f"verify_rerun_archived FAILED ({failures})")
        raise SystemExit(1)
    print("RERUN ARCHIVED OK")
