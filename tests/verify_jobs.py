"""Background job engine verification (offline).

- POST /documents/{id}/translate enqueues a job; worker translates in the
  background (parallel provider calls); progress + token usage recorded
- connector 'Translate now' runs as a job; failures land in job.error
- restart recovery: RUNNING -> failed, QUEUED -> re-enqueued and completed
- retry taxonomy: terminal errors (bad key) skip retries and fail over fast
- /jobs page renders; /jobs/{id}/status JSON; token-authed /api/jobs/{id}
"""

import os
import re
import sys
import time
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_jobs_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Job, JobStatus, Segment
from app.services import jobs
from app.services.connectors import store as cstore
from app.services.providers import (
    MockProvider,
    Provider,
    ProviderSpec,
    register_provider,
    run_completion,
)
from tests._api_fixture import user_api_token


def dx(ps):
    d = Docx()
    for p in ps:
        d.add_paragraph(p)
    b = BytesIO()
    d.save(b)
    return b.getvalue()


def wait_job(c, jid, timeout=30):
    deadline = time.time() + timeout
    d = {}
    while time.time() < deadline:
        d = c.get(f"/jobs/{jid}/status").json()
        if d.get("status") in (JobStatus.COMPLETED, JobStatus.FAILED):
            return d
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish in {timeout}s: {d}")


class TerminalAuthError(Exception):
    status_code = 401


class BadKeyProvider(Provider):
    name = "badkey"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        BadKeyProvider.calls += 1
        raise TerminalAuthError("invalid api key")


class ModelNotServed(RuntimeError):
    """The 2026-10-06 production shape: the engine answers, but the model it was
    configured with is not served. status_code 404 makes it terminal for
    run_completion (no retry/backoff burn) and no fallback is configured on these
    stubs, so every segment fails for real."""

    status_code = 404


class DeadModelProvider(MockProvider):
    """Every call errors with no text, exactly like the unserved model in the report.
    A MockProvider subclass so the call stays spend-exempt and the language guard
    still recognizes it as offline behavior on the partial-failure stub below."""

    name = "deadmodel"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        raise ModelNotServed("the model 'claude-not-here' is not served")


class HalfDeadModelProvider(MockProvider):
    """Real mock output for every segment EXCEPT the one whose source carries the
    FAIL marker, so a partial failure is exercisable end to end."""

    name = "halfdead"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if "FAIL" in str((payload or {}).get("text") or ""):
            raise ModelNotServed("the model 'claude-not-here' is not served")
        return MockProvider().complete(prompt, system=system, task=task, payload=payload, **kw)


def _new_doc(c, name, provider, paragraphs):
    """A fresh segmented project (so no TM can short-circuit the engine) + one doc."""
    r = c.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": provider,
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                dx(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def main() -> None:
    init_db()
    c = TestClient(app)

    # --- retry taxonomy: terminal error -> no retries on primary, straight to fallback
    register_provider("badkey", BadKeyProvider())
    spec = ProviderSpec(
        provider="badkey",
        model="m",
        fallback_provider="mock",
        fallback_model="m2",
        retry_max=2,
        backoff_base=0,
    )
    res = run_completion(spec, "hi", task=None)
    assert res["provider_used"] == "mock", res
    assert BadKeyProvider.calls == 1, (
        f"terminal error must not be retried, got {BadKeyProvider.calls} calls"
    )
    assert res["attempts"] == 2, res  # 1 terminal + 1 fallback success
    assert res["usage"] and res["usage"]["input_tokens"] >= 1, (
        res
    )  # usage always present on success
    print("  retry taxonomy: terminal error skips retries, fails over (usage captured)")

    # --- document translation as a background job ---
    r = c.post(
        "/projects",
        data={
            "name": "Jobs Proj",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                dx(["One sentence.", "Two sentences.", "Three now."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])

    r = c.post(f"/documents/{doc_id}/translate", data={"locale": "fr-FR"}, follow_redirects=False)
    assert r.status_code == 303 and "job=" in r.headers["location"], r.headers
    # digits-only parse: never int() the raw Location tail (a ?job=N&msg=...
    # variant would crash it)
    jid = int(re.search(r"[?&]job=(\d+)", r.headers["location"]).group(1))
    d = wait_job(c, jid)
    assert d["status"] == JobStatus.COMPLETED, d
    assert d["done"] == 3 and d["total"] == 3, d
    assert d["result"]["translated"] == 3, d
    assert d["result"]["tokens_in"] > 0 and d["result"]["tokens_out"] > 0, d
    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "fr-FR")
        ).all()
    assert all((sg.target_text or "").strip() for sg in segs), "job left untranslated segments"
    print(
        f"  translate job: 3/3 segments, tokens in/out = "
        f"{d['result']['tokens_in']}/{d['result']['tokens_out']}"
    )

    # --- #572: a job whose EVERY segment fails is FAILED, not silently completed ---
    # The production API lane (v134, 2026-10-06) posted a translate for an engine whose
    # model is not served: 202 accepted, then the job ended `status completed`,
    # `error ""`, `translated 0`, `errors 2`. An API caller could not tell a dead engine
    # from an empty document. run_sync is the same handler the API/CLI enqueue.
    register_provider("deadmodel", DeadModelProvider())
    register_provider("halfdead", HalfDeadModelProvider())
    dead_doc = _new_doc(c, "Jobs DeadEngine", "deadmodel", ["One sentence.", "Two sentences."])
    job = jobs.run_sync(
        "translate_document", {"document_id": dead_doc, "locale": "fr-FR"}, actor="t"
    )
    assert job.status == JobStatus.FAILED, f"every segment failed, job must be FAILED: {job.status}"
    assert "is not served" in (job.error or ""), f"job.error must carry it: {job.error!r}"
    assert job.result["translated"] == 0 and job.result["errors"] == 2, job.result
    assert job.result["from_segment_tm"] == 0 and job.result["from_full_text_tm"] == 0, job.result
    assert "is not served" in job.result.get("first_error", ""), job.result
    # Same answer through the API door a caller actually polls.
    api = c.get(f"/api/jobs/{job.id}", headers={"X-Loc-Token": user_api_token()}).json()
    assert api["ok"] and api["status"] == JobStatus.FAILED, api
    assert api["error"] and "is not served" in api["error"], api
    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == dead_doc, Segment.target_locale == "fr-FR")
        ).all()
    assert segs and not any((sg.target_text or "").strip() for sg in segs), (
        "a dead engine must leave every segment untranslated"
    )
    print("  #572: every segment failing -> FAILED, engine text in error, stats kept on result")

    # --- #612: a PARTIAL failure is FAILED too (some segments landed, some did not) ---
    # The 2026-10-07 night test: an MT restart's 502 window left 37 of 197 segments failed
    # and the job read `completed`, `error ""`. Now the row says failed, names the count
    # and the first engine error, keeps the stats, and the segments that landed stay saved.
    half_doc = _new_doc(
        c, "Jobs HalfDead", "halfdead", ["One sentence.", "Two FAIL sentences.", "Three now."]
    )
    job = jobs.run_sync(
        "translate_document", {"document_id": half_doc, "locale": "fr-FR"}, actor="t"
    )
    assert job.status == JobStatus.FAILED, f"partial failure must be FAILED: {job.status}"
    assert job.result["translated"] == 2 and job.result["errors"] == 1, job.result
    assert "is not served" in job.result.get("first_error", ""), job.result
    assert "1 of 3 segment(s) failed" in job.error, job.error
    assert "is not served" in job.error and "Retry failed segments" in job.error, job.error
    assert job.message == "2/3 segments translated, 1 failed", job.message
    api = c.get(f"/api/jobs/{job.id}", headers={"X-Loc-Token": user_api_token()}).json()
    assert api["status"] == JobStatus.FAILED and "1 of 3 segment(s) failed" in api["error"], api
    with Session(engine) as s:
        got = [
            (sg.source_text, bool((sg.target_text or "").strip()))
            for sg in s.exec(
                select(Segment).where(
                    Segment.document_id == half_doc, Segment.target_locale == "fr-FR"
                )
            ).all()
        ]
    assert sorted(t for _s, t in got) == [False, True, True], got
    print("  #612: one segment failing of three -> FAILED, count + first error, landed kept")

    # --- #612: the export of that locale is HELD until the failed segments are retried ---
    from app.services import apikeys, security

    user_api_token()  # makes the fixture admin
    _uid = security.get_by_email("connector-fixture@example.test").id
    H = {"X-Loc-Token": apikeys.create_key(_uid, "jobs docs", scopes="api docs")[1]}
    r = c.get(f"/api/documents/{half_doc}/download", headers=H, params={"locale": "fr-FR"})
    assert r.status_code == 409 and r.json().get("held"), (r.status_code, r.text[:200])
    assert "1 unit(s) are still untranslated" in r.json()["error"], r.json()
    assert "allow_held=1" in (r.json().get("export_anyway_url") or ""), r.json()
    r = c.get(f"/documents/{half_doc}/export", params={"locale": "fr-FR"})
    assert r.status_code == 409, r.status_code
    r = c.get(
        f"/api/documents/{half_doc}/download",
        headers=H,
        params={"locale": "fr-FR", "allow_held": 1},
    )
    assert r.status_code == 200, f"allow_held ships the source-filled file: {r.status_code}"
    with Session(engine) as s:
        from app.models import Document as _Doc
        from app.services import export as _export

        assert "still untranslated" in (
            _export.held_locale_reason(s, s.get(_Doc, half_doc), "fr-FR") or ""
        )
        half_pid = s.get(_Doc, half_doc).project_id
    print("  #612: partial locale export -> 409 held (web + /api), allow_held=1 ships it")

    # --- #612 gate: the same hold on the project ZIP (skip-and-report) and CLI doc export ---
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as cli_main
    from app.services import delivery as _delivery

    with Session(engine) as s:
        _data, _n, _ct, skipped = _delivery.export_project(s, half_pid, locales=["fr-FR"], zip=True)
    assert any(x["doc_id"] == half_doc and "still untranslated" in x["reason"] for x in skipped), (
        skipped
    )
    _out, _err = io.StringIO(), io.StringIO()
    _xpath = os.path.join(os.environ.get("LOC_TMS_DATA_DIR", "."), "half_held.docx")
    with redirect_stdout(_out), redirect_stderr(_err):
        try:
            rc = cli_main(
                ["doc", "export", str(half_doc), "--locale", "fr-FR", "--out", _xpath, "--force"]
            )
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    assert rc not in (0, None) and not os.path.exists(_xpath), (rc, _err.getvalue())
    assert "still untranslated" in (_out.getvalue() + _err.getvalue()), _err.getvalue()

    # --- #612 gate: a re-run that is only QUEUED (parked at an engine cap) or a live-stream
    #     reservation left RUNNING does not release the hold before the units are filled ---
    with Session(engine) as s:
        parked = Job(
            kind="translate_document",
            status=JobStatus.QUEUED,
            payload={"document_id": half_doc, "locale": "fr-FR"},
        )
        s.add(parked)
        s.commit()
        parked_id = parked.id
    r = c.get(f"/api/documents/{half_doc}/download", headers=H, params={"locale": "fr-FR"})
    assert r.status_code == 409, f"an in-flight re-run must not lift the hold: {r.status_code}"
    with Session(engine) as s:
        s.delete(s.get(Job, parked_id))
        s.commit()
    print("  #612 gate: project ZIP skips it, CLI doc export refuses it, a queued re-run keeps it")

    # --- #612: resume ("Retry failed segments") translates ONLY the missing unit and
    #     releases the hold; a newer clean run supersedes the failed one ---
    register_provider("halfdead", MockProvider())
    again = jobs.run_sync(
        "translate_document", {"document_id": half_doc, "locale": "fr-FR"}, actor="t"
    )
    assert again.status == JobStatus.COMPLETED and not again.error, (again.status, again.error)
    assert again.result["translated"] == 1 and again.result["errors"] == 0, again.result
    r = c.get(f"/api/documents/{half_doc}/download", headers=H, params={"locale": "fr-FR"})
    assert r.status_code == 200, f"after the retry the export is released: {r.status_code}"
    register_provider("halfdead", HalfDeadModelProvider())
    print("  #612: re-running translate resumes only the failed segment and lifts the hold")

    # --- #612: the CLI door follows the same rule (non-zero exit, FAILED job row) ---
    cli_doc = _new_doc(
        c, "Jobs HalfDead CLI", "halfdead", ["One sentence.", "Two FAIL sentences.", "Three now."]
    )
    _out, _err = io.StringIO(), io.StringIO()
    with redirect_stdout(_out), redirect_stderr(_err):
        try:
            rc = cli_main(["doc", "translate", str(cli_doc), "--locale", "fr-FR"])
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    assert rc not in (0, None), f"a partial failure must exit non-zero: {rc}"
    assert "1 of 3 segment(s) failed" in (_out.getvalue() + _err.getvalue()), _err.getvalue()
    with Session(engine) as s:
        cj = s.exec(
            select(Job).where(Job.kind == "translate_document").order_by(Job.id.desc())
        ).first()
    assert cj.status == JobStatus.FAILED and "1 of 3" in cj.error, (cj.status, cj.error)
    print("  #612: CLI doc translate with one failed segment -> non-zero exit + FAILED job row")

    # --- #572 review: the LIVE stream settles by the same rule (all failed -> FAILED) ---
    dead_stats = {"translated": 0, "errors": 2, "from_segment_tm": 0, "first_error": "no model"}
    jid = jobs.reserve_live_translate(dead_doc, "fr-FR", actor="t")
    jobs.settle_live_translate(jid, dict(dead_stats), done=2)
    with Session(engine) as s:
        lj = s.get(Job, jid)
    assert lj.status == JobStatus.FAILED and lj.error == "no model", (lj.status, lj.error)
    assert lj.result["errors"] == 2, lj.result
    jid = jobs.reserve_live_translate(half_doc, "fr-FR", actor="t")
    jobs.settle_live_translate(jid, {**dead_stats, "translated": 2, "errors": 1}, done=3)
    with Session(engine) as s:
        lj = s.get(Job, jid)
    assert lj.status == JobStatus.FAILED and "1 segment(s) failed" in lj.error, (
        lj.status,
        lj.error,
    )
    jid = jobs.reserve_live_translate(half_doc, "fr-FR", actor="t")
    jobs.settle_live_translate(jid, {"translated": 3, "errors": 0}, done=3)
    with Session(engine) as s:
        lj = s.get(Job, jid)
    assert lj.status == JobStatus.COMPLETED and not lj.error, (lj.status, lj.error)
    assert jobs.all_failed_error({"errors": 1, "from_segment_tm": 1}) == ""
    assert jobs.all_failed_error({"errors": 1}) == "every segment failed"
    assert jobs.all_failed_error({}) == "" and jobs.all_failed_error(None) == ""
    rec = jobs.record_sync_job(
        "translate_document",
        {"document_id": dead_doc, "locale": "fr-FR"},
        result=dead_stats,
        status=JobStatus.FAILED,
        error="no model",
    )
    assert rec.status == JobStatus.FAILED and rec.error == "no model", rec
    print("  #572 review: live stream and CLI job row follow the same all-failed rule")

    # --- and a document with NOTHING to translate keeps its own clean failure ---
    none = jobs.run_sync(
        "translate_document", {"document_id": dead_doc, "locale": "de-DE"}, actor="t"
    )
    assert none.status == JobStatus.FAILED and "No segments" in (none.error or ""), (
        none.status,
        none.error,
    )
    print("  #572: zero segments for the locale still fails with the 'No segments' message")

    # --- connector translate as a job (patched translate_now: success + failure) ---
    real_translate_now = cstore.translate_now
    set_pieces(
        "app.services.connectors.store",
        "translate_now",
        lambda cid, actor="", transport=None: (
            True,
            "ok: 5 items",
            {"translated": 5},
        ),
    )
    try:
        jid = jobs.submit(
            "connector_translate", {"connection_id": 1, "actor": "test"}, actor="test"
        )
        d = wait_job(c, jid)
        assert d["status"] == JobStatus.COMPLETED and d["result"]["translated"] == 5, d
        set_pieces(
            "app.services.connectors.store",
            "translate_now",
            lambda cid, actor="", transport=None: (False, "boom: API down", {}),
        )
        jid = jobs.submit(
            "connector_translate", {"connection_id": 1, "actor": "test"}, actor="test"
        )
        d = wait_job(c, jid)
        assert d["status"] == JobStatus.FAILED and "API down" in d["error"], d
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real_translate_now)
    print("  connector job: success recorded; failure lands in job.error")

    # --- #24 regression: a bad connection id is a CONTROLLED failure, not a crash ---
    # translate_now(999) returns (False, "No such connection.", {}); the handler raises
    # ConnectorRunError, which _execute must give the CLEAN-failure path: FAILED with a
    # one-line error and NO log.exception traceback (the raw-traceback bug the operator
    # saw from `job run connector_translate --payload connection_id=999`). Pre-fix this
    # was a bare RuntimeError -> the except-Exception crash branch -> log.exception dump
    # + a "RuntimeError: " type-prefixed error. run_sync runs it in-thread (the one-shot
    # CLI path). We assert both the clean status/message AND that no traceback was logged.
    real_log_exception = jobs.log.exception
    exc_calls = []
    jobs.log.exception = lambda *a, **k: exc_calls.append((a, k))
    try:
        job = jobs.run_sync("connector_translate", {"connection_id": 999, "actor": "t"}, actor="t")
    finally:
        jobs.log.exception = real_log_exception
    assert job.status == JobStatus.FAILED, job.status
    assert job.error == "No such connection.", (
        f"clean path must record the bare message, not a type-prefixed crash: {job.error!r}"
    )
    assert not exc_calls, (
        f"controlled connector failure must NOT log.exception a traceback: {exc_calls}"
    )
    # And a genuinely bad kind (no webhook dispatch) is ALSO clean, not a crash:
    exc_calls.clear()
    jobs.log.exception = lambda *a, **k: exc_calls.append((a, k))
    try:
        job2 = jobs.run_sync(
            "connector_translate",
            {"connection_id": 999, "actor": "t", "webhook_record_ids": ["x"]},
            actor="t",
        )
    finally:
        jobs.log.exception = real_log_exception
    assert job2.status == JobStatus.FAILED and not exc_calls, (job2.status, exc_calls)
    print("  #24: bad connection id / unroutable webhook fail CLEAN (no traceback dump)")

    # --- restart recovery ---
    jobs.HANDLERS["test_noop"] = lambda payload, progress: {"ok": True}
    with Session(engine) as s:
        running = Job(kind="test_noop", status=JobStatus.RUNNING)
        queued = Job(kind="test_noop", status=JobStatus.QUEUED)
        s.add(running)
        s.add(queued)
        s.commit()
        s.refresh(running)
        s.refresh(queued)
        rid, qid = running.id, queued.id
    touched = jobs.recover_interrupted()
    assert touched >= 2, touched
    with Session(engine) as s:
        assert s.get(Job, rid).status == JobStatus.FAILED, "RUNNING must fail on restart"
        assert "restart" in s.get(Job, rid).error
    d = wait_job(c, qid)
    assert d["status"] == JobStatus.COMPLETED, "QUEUED must be re-enqueued and run"
    print("  restart recovery: RUNNING -> failed, QUEUED -> re-run to completion")

    # --- pages + APIs ---
    page = c.get("/jobs").text
    # /jobs is now the unified "Execution history" log (jobs + workflow runs).
    assert "Execution history" in page and "translate_document" in page, "jobs page missing rows"
    assert c.get("/jobs/999999/status").status_code == 404
    token = user_api_token()
    assert c.get("/api/jobs/1").status_code == 401, "API job status must require token"
    d = c.get(f"/api/jobs/{jid}", headers={"X-Loc-Token": token}).json()
    assert d["ok"] and d["status"] in (JobStatus.COMPLETED, JobStatus.FAILED), d
    print("  jobs page renders; status JSON; token-authed API job status")

    print("JOBS OK")


if __name__ == "__main__":
    main()
