"""QA Part-2 P-C5: transient SQLite writer-lock is now surfaced HONESTLY and ridden
out with a bounded, jittered retry (offline, hermetic).

Root finding (the planning notes, C5):
at ~15+ concurrent full runs the single-writer SQLite DB exhausts its 5s busy_timeout
and raises `sqlite3.OperationalError: database is locked` on a write (the QA repro hit
`INSERT INTO steprun`). Data INTEGRITY already held (atomic rollback, segments stay
`new`), but TWO gaps remained:
  (1) the crash surfaced to the API as `error: null` -- indistinguishable from a real
      failure, because `_run_in_background`'s crash handler marked the run FAILED
      without recording WHY, so `observe_run._failure_reason` had nothing to report;
  (2) no application-level retry rode out a merely-transient lock.

THE FIXES under test:
  * db.is_locked_error / db.commit_with_retry -- a bounded jittered retry that
    de-synchronizes a thundering herd of writers (busy_timeout alone cannot: its
    retry schedule is un-jittered, so the losers stay in lockstep).
  * orchestrator: the StepRun-insert write seam commits through commit_with_retry, and
    the crash handler stamps a DISTINCT retryable note (WRITE_CONTENTION_NOTE) on a
    transient lock -- or a bounded exception note on any other crash -- so every
    surface reports an honest reason instead of `error: null`.

GROUPS
  [A] is_locked_error: matches SQLITE_BUSY/LOCKED phrases, rejects a non-lock
      OperationalError and any non-OperationalError. NARROW by design.
  [B] commit_with_retry: a transient lock that clears is retried to success; the
      staged work is re-applied after the intervening rollback and really persists.
  [C] commit_with_retry: a lock that NEVER clears re-raises the OperationalError after
      the bounded attempts (so the caller's crash handler still fails the run), and a
      NON-lock OperationalError is re-raised IMMEDIATELY (no wasted retries).
  [D] HONEST ERROR end-to-end: a run whose execution dies on a transient lock is FAILED
      with WRITE_CONTENTION_NOTE, and observe_run surfaces that note as the `closed`
      event's `error` (NOT null). A non-lock crash records its bounded reason instead.
  [E] HAPPY PATH byte-stability: an ordinary run still completes and drives the seam
      through commit_with_retry (the wiring is live, and the uncontended path is a
      plain single commit).
  [F] PREREQUISITE: WAL journal mode is ON and the explicit busy_timeout floor is
      installed (both raise the concurrent-writer ceiling the retry sits on top of).

Hermetic: throwaway LOC_TMS_DATA_DIR, cloud keys blanked, MockProvider, ASCII prints.
"""

import os
import sys
import time as _time
from datetime import datetime
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import monkeypatch_pieces

init_env("loctms_writecontention_")

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from app import db as _db
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, settings


def _locked_error(stmt="INSERT INTO steprun ...") -> OperationalError:
    """A SQLAlchemy OperationalError shaped exactly like pysqlite's SQLITE_BUSY: the
    lock phrase lives on `.orig`, which is what is_locked_error inspects."""
    return OperationalError(stmt, {}, Exception("database is locked"))


# --------------------------------------------------------------------------- #
# [A] is_locked_error — narrow by design
# --------------------------------------------------------------------------- #
def test_is_locked_error_classifies():
    assert _db.is_locked_error(_locked_error()) is True, "SQLITE_BUSY must match"
    assert (
        _db.is_locked_error(OperationalError("x", {}, Exception("database table is locked")))
        is True
    ), "SQLITE_LOCKED (table is locked) must match"
    # A non-lock OperationalError (schema/IO) is NOT transient -> must fail the run.
    assert (
        _db.is_locked_error(OperationalError("x", {}, Exception("no such table: steprun"))) is False
    ), "a non-lock OperationalError must not be treated as transient"
    # Non-OperationalError never matches.
    assert _db.is_locked_error(IntegrityError("x", {}, Exception("UNIQUE"))) is False
    assert _db.is_locked_error(ValueError("nope")) is False
    print("  [A] is_locked_error matches BUSY/LOCKED, rejects non-lock + non-Operational")


# --------------------------------------------------------------------------- #
# [B] commit_with_retry — a transient lock that clears is retried to success
# --------------------------------------------------------------------------- #
def test_commit_with_retry_succeeds_after_transient_lock(monkeypatch):
    # A fresh project row is the unit of work; the real commit is wrapped so the first
    # two attempts raise a transient lock and the third succeeds. The retry must
    # RE-STAGE (rollback expunged the pending row) and the row must really land.
    with Session(engine) as s:
        real_commit = s.commit
        calls = {"n": 0}

        def flaky_commit():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise _locked_error()
            return real_commit()

        monkeypatch.setattr(s, "commit", flaky_commit)
        slept = []
        monkeypatch.setattr(_db.time, "sleep", lambda d: slept.append(d))

        holder = {}

        def _stage():
            # Build a NEW row each pass so a re-stage after expunge is clean.
            p = Project(name="wc-retry", source_locale="en-US")
            s.add(p)
            holder["p"] = p

        _db.commit_with_retry(s, stage=_stage, attempts=5)
        assert calls["n"] == 3, ("commit retried until it cleared", calls["n"])
        assert len(slept) == 2, ("one bounded backoff per lost attempt", slept)
        assert all(0 < d <= _db.WRITE_RETRY_MAX_S * 1.5 for d in slept), (
            "backoff jittered within the cap",
            slept,
        )
        pid = holder["p"].id

    with Session(engine) as s:
        assert s.get(Project, pid) is not None, "the retried write must have persisted"
    print("  [B] commit_with_retry rode out a transient lock and the row persisted")


# --------------------------------------------------------------------------- #
# [C] commit_with_retry — exhaustion re-raises; a non-lock error is immediate
# --------------------------------------------------------------------------- #
def test_commit_with_retry_exhausts_and_reraises(monkeypatch):
    with Session(engine) as s:
        monkeypatch.setattr(_db.time, "sleep", lambda d: None)
        attempts = {"n": 0}

        def always_locked():
            attempts["n"] += 1
            raise _locked_error()

        monkeypatch.setattr(s, "commit", always_locked)
        raised = False
        try:
            _db.commit_with_retry(s, stage=lambda: None, attempts=3)
        except OperationalError as exc:
            raised = _db.is_locked_error(exc)
        assert raised, "a lock that never clears must re-raise the OperationalError"
        assert attempts["n"] == 3, ("exactly `attempts` tries, no more", attempts["n"])

    # A NON-lock OperationalError must NOT be retried at all.
    with Session(engine) as s:
        monkeypatch.setattr(_db.time, "sleep", lambda d: None)
        tries = {"n": 0}

        def non_lock():
            tries["n"] += 1
            raise OperationalError("x", {}, Exception("no such table: steprun"))

        monkeypatch.setattr(s, "commit", non_lock)
        got = False
        try:
            _db.commit_with_retry(s, stage=lambda: None, attempts=5)
        except OperationalError:
            got = True
        assert got and tries["n"] == 1, (
            "a non-lock OperationalError is raised on the first attempt",
            tries["n"],
        )
    print("  [C] exhaustion re-raises the lock; a non-lock error is raised immediately")


# --------------------------------------------------------------------------- #
# helpers for the end-to-end run tests
# --------------------------------------------------------------------------- #
def _make_runnable(name):
    """A minimal 1-step MockProvider workflow over a 2-segment doc. Returns
    (workflow_id, doc_id, locale)."""
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        doc = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, txt in enumerate(("Hello world.", "Goodbye now.")):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=txt,
                    status=SegStatus.NEW,
                )
            )
        wf = Workflow(name=name + "-wf", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="Translate",
                provider="mock",
                step_type=StepType.LLM_AGENT,
                prompt_text="Translate: {text}",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
            )
        )
        s.commit()
        return wf.id, doc.id, "es-ES"


def _wait_terminal(run_id, timeout=20.0):
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        with Session(engine) as s:
            run = s.get(WorkflowRun, run_id)
            if run is not None and run.status in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            ):
                return run.status
        _time.sleep(0.05)
    return None


def _closed_error(run_id):
    """The `error` the observer would hand the API for this terminal run."""
    for _run, ev in orchestrator.observe_run(run_id, poll_interval=0.02, max_seconds=5.0):
        if ev.get("type") == "closed":
            return ev.get("error")
    return None


# --------------------------------------------------------------------------- #
# [D] HONEST ERROR end-to-end — a lock crash surfaces WRITE_CONTENTION_NOTE
# --------------------------------------------------------------------------- #
def test_lock_crash_surfaces_honest_retryable_error(monkeypatch):
    wf_id, doc_id, locale = _make_runnable("wc-honest")

    # Force the background execution to die on a TRANSIENT lock (as the 18-way flood
    # did). `_run_in_background._work` calls run_workflow_stream by module name, so a
    # stub that raises there drives the exact crash-handler path under test.
    def boom(*a, **k):
        raise _locked_error("INSERT INTO steprun ...")

    monkeypatch_pieces(monkeypatch, "app.services.orchestrator", "run_workflow_stream", boom)
    with Session(engine) as s:
        run_id = orchestrator.start_run(s, wf_id, doc_id, locale)
    status = _wait_terminal(run_id)
    assert status == RunStatus.FAILED, ("the lock crash must FAIL the run", status)
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert run.notes and "please retry" in run.notes.lower(), (
            "a transient lock must stamp the retryable note, not error:null",
            run.notes,
        )
        assert run.notes == orchestrator.WRITE_CONTENTION_NOTE, ("the exact honest note", run.notes)
        # Integrity: no segment was written (segments stay NEW, blank target).
        segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        assert all(sg.status == SegStatus.NEW and not (sg.target_text or "") for sg in segs), (
            "segments must stay NEW on a failed run"
        )
    err = _closed_error(run_id)
    assert err == orchestrator.WRITE_CONTENTION_NOTE, (
        "observe_run must surface the honest note (NOT null)",
        err,
    )
    print("  [D] a transient-lock crash surfaces the retryable note end-to-end (not null)")


def test_non_lock_crash_records_bounded_reason(monkeypatch):
    wf_id, doc_id, locale = _make_runnable("wc-nonlock")

    def boom(*a, **k):
        raise RuntimeError("kaboom in the engine room")

    monkeypatch_pieces(monkeypatch, "app.services.orchestrator", "run_workflow_stream", boom)
    with Session(engine) as s:
        run_id = orchestrator.start_run(s, wf_id, doc_id, locale)
    assert _wait_terminal(run_id) == RunStatus.FAILED
    err = _closed_error(run_id)
    assert err and "kaboom in the engine room" in err and "RuntimeError" in err, (
        "a non-lock crash records its bounded exception reason, not null",
        err,
    )
    assert "please retry" not in (err or "").lower(), (
        "a real (non-lock) failure must NOT masquerade as retryable contention"
    )
    print("  [D2] a non-lock crash records its bounded exception reason (not null)")


# --------------------------------------------------------------------------- #
# [E] HAPPY PATH — an ordinary run still completes and drives the seam via retry
# --------------------------------------------------------------------------- #
def test_happy_path_completes_and_uses_retry_seam(monkeypatch):
    wf_id, doc_id, locale = _make_runnable("wc-happy")
    real = _db.commit_with_retry
    seam = {"n": 0}

    def counting(*a, **k):
        seam["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(_db, "commit_with_retry", counting)
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, locale)
    assert summary["status"] == "completed", ("an uncontended run completes", summary)
    assert seam["n"] >= 1, "the StepRun-insert seam must route through commit_with_retry"
    with Session(engine) as s:
        segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        assert all((sg.target_text or "").strip() for sg in segs), (
            "the completed run wrote a target for every segment"
        )
    print("  [E] an ordinary run completes and drives the write seam through the retry")


# --------------------------------------------------------------------------- #
# [D3] a CANCEL that races the crash-handler retry is NOT clobbered to FAILED
#      (adversarial review, MEDIUM: the terminal-status re-check lives INSIDE the
#      staged write, so a rollback-expired re-read sees the committed cancel).
# --------------------------------------------------------------------------- #
def test_cancel_during_retry_is_not_clobbered(monkeypatch):
    wf_id, doc_id, locale = _make_runnable("wc-cancel-race")

    def boom(*a, **k):
        raise _locked_error("INSERT INTO steprun ...")

    monkeypatch_pieces(monkeypatch, "app.services.orchestrator", "run_workflow_stream", boom)

    real_cwr = _db.commit_with_retry
    fired = {"n": 0}

    def racing_cwr(session, *, stage=None, attempts=_db.WRITE_RETRY_ATTEMPTS, keep_loaded=False):
        # R2-F1: start_run's OWN launch INSERT now also routes through commit_with_retry
        # (via commit_launch), so the FIRST call here is the launch write, made BEFORE the
        # run row is committed -- pass it straight through (no run row exists to race yet).
        # The crash handler's FAILED-marking write is the first call at which a run row
        # EXISTS; that is the one we inject the cancel into. Simulate a cancel landing AFTER
        # its outer status read but before the staged write commits, then expire the handler's
        # session so its in-stage re-read behaves like the post-rollback reload the real retry
        # would do -- and sees the committed CANCELLED.
        if fired["n"] == 0:
            with Session(engine) as cs:
                r = cs.exec(
                    select(WorkflowRun)
                    .where(WorkflowRun.workflow_id == wf_id, WorkflowRun.document_id == doc_id)
                    .order_by(WorkflowRun.id.desc())
                ).first()
                if r is None:
                    # start_run's launch insert (run not committed yet) -- let it commit.
                    return real_cwr(
                        session, stage=stage, attempts=attempts, keep_loaded=keep_loaded
                    )
                fired["n"] += 1
                r.status = RunStatus.CANCELLED
                r.finished_at = datetime.utcnow()
                cs.add(r)
                cs.commit()
            session.expire_all()
        return real_cwr(session, stage=stage, attempts=attempts, keep_loaded=keep_loaded)

    monkeypatch.setattr(_db, "commit_with_retry", racing_cwr)
    with Session(engine) as s:
        run_id = orchestrator.start_run(s, wf_id, doc_id, locale)
    _wait_terminal(run_id)
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.CANCELLED, (
            "a cancel that raced the crash handler must survive, not flip to FAILED",
            run.status,
        )
        assert orchestrator.WRITE_CONTENTION_NOTE not in (run.notes or ""), (
            "the no-op stage must not stamp the contention note over a cancelled run"
        )
    print("  [D3] a cancel racing the crash-handler retry is NOT clobbered to FAILED")


# --------------------------------------------------------------------------- #
# [D4] WRITE_CONTENTION_NOTE outranks a benign prior-step note in _failure_reason
#      (adversarial review, LOW: a multi-step run that locks on a LATER step must not
#      surface a hygiene/LQA note from an earlier COMPLETED step instead of "retry").
# --------------------------------------------------------------------------- #
def test_contention_note_outranks_benign_step_note():
    # run.notes carries the crash-stamped contention note; an earlier COMPLETED step's
    # sr.error carries a benign informational note that USED to shadow it.
    run = SimpleNamespace(notes=orchestrator.WRITE_CONTENTION_NOTE)
    step_runs = [
        SimpleNamespace(error="LQA safety floor: 1 TM segment(s) protected"),
        SimpleNamespace(error=""),
    ]
    reason = orchestrator._failure_reason(run, step_runs)
    assert reason == orchestrator.WRITE_CONTENTION_NOTE, (
        "the retryable contention note must outrank a benign prior-step note",
        reason,
    )
    # Precedence for a NON-contention failure is unchanged: the last non-empty step
    # error still wins over run.notes.
    run2 = SimpleNamespace(notes="some unrelated recovery note")
    assert orchestrator._failure_reason(run2, step_runs).startswith("LQA safety floor"), (
        "non-contention failures keep the last-step-error precedence"
    )
    print("  [D4] WRITE_CONTENTION_NOTE outranks a benign prior-step note in _failure_reason")


# --------------------------------------------------------------------------- #
# [F] PREREQUISITE — WAL on + explicit busy_timeout floor installed
# --------------------------------------------------------------------------- #
def test_wal_and_busy_timeout_installed():
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy = int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar())
    assert str(mode).lower() == "wal", ("WAL raises the concurrent-writer ceiling", mode)
    assert busy == _db.BUSY_TIMEOUT_MS and busy > 0, (
        "the explicit busy_timeout floor is installed",
        busy,
        _db.BUSY_TIMEOUT_MS,
    )
    print("  [F] prerequisites hold: journal_mode=WAL, busy_timeout=%d ms" % busy)  # noqa: UP031


# --------------------------------------------------------------------------- #
# [G] PROMPT-TAP write paths — R1-F1 (volume-flood): the tap enrichment/envelope
#     writes now ride out a transient lock via commit_with_retry and DEGRADE (one
#     WARN naming the taprun, the write dropped) instead of raising the unhandled
#     `database is locked` traceback the 25-way flood surfaced on
#     `UPDATE taprun SET detail=?`. The Tap is OBSERVABILITY: a shed write must never
#     raise into the run/thread that hosts it.
# --------------------------------------------------------------------------- #
import logging as _logging


class _ListHandler(_logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture(logger_name):
    logger = _logging.getLogger(logger_name)
    h = _ListHandler()
    logger.addHandler(h)
    return logger, h


def _make_terminal_envelope():
    """A minimal TERMINAL TapAction + TapRun (no document needed) so enrich() runs its
    read-then-write DETAIL path fast + hermetic. Returns (action_id, run_id)."""
    from app.models import TAP_SCHEMA_VERSION, TapAction, TapActionKind, TapRun, TapStatus
    from app.services import tap_envelope

    aid = tap_envelope.new_id()
    rid = tap_envelope.new_id()
    now = datetime.utcnow()
    with Session(engine) as s:
        s.add(
            TapAction(
                id=aid,
                schema_version=TAP_SCHEMA_VERSION,
                kind=TapActionKind.DIRECT,
                status=TapStatus.COMPLETED,
                source_locale="en-US",
                started_at=now,
                finished_at=now,
                created_at=now,
            )
        )
        s.add(
            TapRun(
                id=rid,
                schema_version=TAP_SCHEMA_VERSION,
                action_id=aid,
                target_locale="es-ES",
                source_locale="en-US",
                status=TapStatus.COMPLETED,
                started_at=now,
                finished_at=now,
                created_at=now,
            )
        )
        s.commit()
    return aid, rid


def test_commit_tap_write_rides_out_transient_lock(monkeypatch):
    """A tap detail write that loses the lock twice is RETRIED (re-staged) to success --
    the same jittered retry the steprun seam got, now on the taprun write path."""
    from app.models import TapRun
    from app.services import tap_envelope

    _aid, rid = _make_terminal_envelope()
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)
    with Session(engine) as s:
        real_commit = s.commit
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise _locked_error("UPDATE taprun SET detail=? WHERE taprun.id = ...")
            return real_commit()

        monkeypatch.setattr(s, "commit", flaky)

        def _stage():
            run = s.get(TapRun, rid)
            run.detail = {"probe": 1}
            s.add(run)

        ok = tap_envelope.commit_tap_write(s, _stage, what=f"run-detail for taprun {rid}")
        assert ok is True and calls["n"] == 3, ("rode out the transient lock", ok, calls)
    with Session(engine) as s:
        assert s.get(TapRun, rid).detail == {"probe": 1}, "the retried detail must persist"
    print("  [G] a tap detail write rides out a transient lock and persists")


def test_commit_tap_write_degrades_on_persistent_lock(monkeypatch):
    """A lock that NEVER clears must DEGRADE: one WARN naming the taprun, the write
    dropped, and NO exception propagated (the R1-F1 fix -- no unhandled traceback)."""
    from app.models import TapRun
    from app.services import tap_envelope

    _aid, rid = _make_terminal_envelope()
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)
    logger, handler = _capture("loc_tms.tap_envelope")
    prev_level = logger.level
    logger.setLevel(_logging.WARNING)
    raised = False
    ok = None
    try:
        with Session(engine) as s:

            def always_locked():
                raise _locked_error("UPDATE taprun SET detail=? WHERE taprun.id = ...")

            monkeypatch.setattr(s, "commit", always_locked)

            def _stage():
                run = s.get(TapRun, rid)
                run.detail = {"x": 1}
                s.add(run)

            try:
                ok = tap_envelope.commit_tap_write(
                    s,
                    _stage,
                    what=f"run-detail for taprun {rid}",
                )
            except Exception:
                raised = True
        assert raised is False and ok is False, (
            "a persistent lock DEGRADES, never raises",
            raised,
            ok,
        )
        msgs = [rec.getMessage() for rec in handler.records if rec.levelno == _logging.WARNING]
        assert any("shed under sustained write contention" in m and rid in m for m in msgs), (
            "the degrade logs ONE WARN naming the taprun id",
            msgs,
        )
        with Session(engine) as s:
            assert s.get(TapRun, rid).detail is None, "the shed write dropped the detail"
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
    print("  [G2] a persistent lock degrades (WARN + drop), never raises a traceback")


def test_enrich_never_raises_under_persistent_lock(monkeypatch):
    """enrich() (the R1-F1 seam itself) sheds BOTH its writes and returns normally when
    the DB stays locked -- the observer never takes the run down with it."""
    from app.models import TapRun
    from app.services import tap_enrich

    aid, rid = _make_terminal_envelope()

    def always_lock(session, *, stage=None, attempts=_db.WRITE_RETRY_ATTEMPTS):
        if stage is not None:
            stage()
        raise _locked_error("UPDATE taprun SET detail=? WHERE taprun.id = ...")

    monkeypatch.setattr(_db, "commit_with_retry", always_lock)
    # MUST NOT raise (a plain call; any propagated exception fails the suite).
    tap_enrich.enrich(aid, rid, target_locale="es-ES")
    with Session(engine) as s:
        run = s.get(TapRun, rid)
        assert run is not None and run.detail is None, (
            "enrich shed the detail write; the row survived intact"
        )
    print("  [G3] enrich() sheds its writes and never raises when the DB stays locked")


def test_enrich_happy_path_writes_detail_via_retry_seam(monkeypatch):
    """On the uncontended path enrich() still POPULATES detail, and its write routes
    through commit_with_retry (the wire is live, byte-identical to a plain commit)."""
    from app.models import TapRun
    from app.services import tap_enrich

    aid, rid = _make_terminal_envelope()
    real = _db.commit_with_retry
    seam = {"n": 0}

    def counting(session, *, stage=None, attempts=_db.WRITE_RETRY_ATTEMPTS):
        seam["n"] += 1
        return real(session, stage=stage, attempts=attempts)

    monkeypatch.setattr(_db, "commit_with_retry", counting)
    tap_enrich.enrich(aid, rid, target_locale="es-ES")
    assert seam["n"] >= 1, "the detail write must route through commit_with_retry"
    with Session(engine) as s:
        run = s.get(TapRun, rid)
        assert run.detail is not None and run.detail.get("target_locale") == "es-ES", (
            "enrich populated the run detail on the happy path",
            run.detail,
        )
    print("  [G4] enrich() populates detail through the retry seam on the happy path")


# --------------------------------------------------------------------------- #
# [H] LAUNCH write paths — R2-F1 (volume-flood): the run/batch/resume ROW a POST
#     creates now rides out a transient lock via commit_launch and, on EXHAUSTION,
#     raises the HONEST retryable WriteContentionError (a 503 signal) instead of the
#     raw `database is locked` 500 the 25-70-way flood surfaced at
#     `orchestrator.start_run`'s `session.commit()` (INSERT INTO workflowrun). Distinct
#     from a validation 4xx, and NOT a 500 traceback.
# --------------------------------------------------------------------------- #
def test_commit_launch_rides_out_transient_lock(monkeypatch):
    """commit_launch is byte-identical to commit_with_retry on the happy path AND when a
    transient lock clears within the bounded attempts — the staged row really persists."""
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)
    with Session(engine) as s:
        real_commit = s.commit
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise _locked_error("INSERT INTO workflowrun ...")
            return real_commit()

        monkeypatch.setattr(s, "commit", flaky)
        holder = {}

        def _stage():
            p = Project(name="wc-launch-ok", source_locale="en-US")
            s.add(p)
            holder["p"] = p

        _db.commit_launch(s, stage=_stage, what="launch probe", attempts=5)
        assert calls["n"] == 3, ("the launch commit rode out two transient locks", calls)
        pid = holder["p"].id
    with Session(engine) as s:
        assert s.get(Project, pid) is not None, "the retried launch write must persist"
    print("  [H1] commit_launch rode out a transient lock and the row persisted")


def test_commit_launch_exhaustion_raises_write_contention_error(monkeypatch):
    """A launch lock that NEVER clears becomes a WriteContentionError — HONEST + RETRYABLE,
    DISTINCT from the raw OperationalError (so the route maps it to 503, not a 500), with a
    positive retry_after and a session left rolled-back/reusable. A NON-lock OperationalError
    still propagates UNCHANGED (never laundered into a retryable 503)."""
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)
    with Session(engine) as s:

        def always_locked():
            raise _locked_error("INSERT INTO workflowrun ...")

        monkeypatch.setattr(s, "commit", always_locked)
        raised = None
        try:
            _db.commit_launch(s, stage=lambda: None, attempts=3)
        except _db.WriteContentionError as exc:
            raised = exc
        assert raised is not None, "a persistent launch lock must raise WriteContentionError"
        assert not isinstance(raised, OperationalError), (
            "WriteContentionError must be DISTINCT from OperationalError (503 vs 500)"
        )
        assert raised.retry_after > 0, ("carries a retry_after hint", raised.retry_after)
        msg = str(raised).lower()
        assert "retry" in msg and "contention" in msg, ("honest retryable wording", msg)

    # A non-lock OperationalError is NOT reshaped — it propagates unchanged.
    with Session(engine) as s:

        def non_lock():
            raise OperationalError("x", {}, Exception("no such table: workflowrun"))

        monkeypatch.setattr(s, "commit", non_lock)
        got = None
        try:
            _db.commit_launch(s, stage=lambda: None, attempts=5)
        except _db.WriteContentionError:
            got = "wce"
        except OperationalError:
            got = "operational"
        assert got == "operational", (
            "a non-lock error must NOT masquerade as retryable contention",
            got,
        )
    print("  [H2] commit_launch exhaustion -> honest WriteContentionError; non-lock propagates")


def test_start_run_launch_rides_out_then_exhausts(monkeypatch):
    """The REAL launch seam: orchestrator.start_run's run-row INSERT rides out a transient
    lock (the run persists), and a lock that never clears raises WriteContentionError — with
    NO run row left behind (the failing INSERT rolled back atomically). _run_in_background is
    stubbed so only the LAUNCH write is under test, never the background execution."""
    wf_id, doc_id, locale = _make_runnable("wc-startrun")
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)
    monkeypatch_pieces(
        monkeypatch, "app.services.orchestrator", "_run_in_background", lambda *a, **k: None
    )

    # (a) rides out two transient locks -> the run launches.
    with Session(engine) as s:
        real_commit = s.commit
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise _locked_error("INSERT INTO workflowrun ...")
            return real_commit()

        monkeypatch.setattr(s, "commit", flaky)
        run_id = orchestrator.start_run(s, wf_id, doc_id, locale)
    assert calls["n"] == 3, ("the launch commit was retried until it cleared", calls)
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None and run.status == RunStatus.RUNNING, (
            "the retried launch produced a live run row",
            run,
        )
    print("  [H3] start_run rode out a transient lock and the run launched")

    # (b) a persistent lock -> WriteContentionError, and NO orphan run row for a 2nd doc.
    wf2, doc2, loc2 = _make_runnable("wc-startrun-503")
    with Session(engine) as s:

        def always_locked():
            raise _locked_error("INSERT INTO workflowrun ...")

        monkeypatch.setattr(s, "commit", always_locked)
        raised = None
        try:
            orchestrator.start_run(s, wf2, doc2, loc2)
        except _db.WriteContentionError as exc:
            raised = exc
        assert raised is not None, "a persistent launch lock must raise WriteContentionError"
    with Session(engine) as s:
        leftover = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc2)).all()
        assert leftover == [], ("no partial run row survives an exhausted launch", leftover)
    print("  [H4] start_run exhaustion raised WriteContentionError and left no orphan run")


def test_launch_contention_surfaces_honest_503_over_http():
    """END-TO-END SHAPE: a launch that raises WriteContentionError is rendered by the app's
    central handler as an HONEST 503 JSON ({ok:false, error, retry_after} + Retry-After
    header), NOT a 500 traceback — and it is DISTINGUISHABLE from a validation 4xx (different
    status, and a validation error carries no retry_after). Exercised through the REAL handler
    (app.main._write_contention_error) over Starlette's exception machinery."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from app.main import _write_contention_error

    tmpapp = FastAPI()
    tmpapp.add_exception_handler(_db.WriteContentionError, _write_contention_error)

    @tmpapp.post("/api/workflows/{wid}/run")
    def _launch(wid: int):
        raise _db.WriteContentionError(f"launch run for workflow {wid}")

    @tmpapp.post("/api/workflows/{wid}/run-badlocale")
    def _bad(wid: int):
        # a VALIDATION refusal (bad locale) is a plain 400 — it must stay distinguishable.
        return JSONResponse({"ok": False, "error": "locale 'zz' not on document"}, status_code=400)

    client = TestClient(tmpapp, raise_server_exceptions=False)
    r = client.post("/api/workflows/7/run")
    assert r.status_code == 503, ("honest 503, not a 500 traceback", r.status_code)
    body = r.json()
    assert body.get("ok") is False, body
    assert "retry" in (body.get("error") or "").lower(), body
    assert body.get("retry_after") == _db.LAUNCH_CONTENTION_RETRY_AFTER_S, body
    assert r.headers.get("Retry-After") == str(_db.LAUNCH_CONTENTION_RETRY_AFTER_S), dict(r.headers)
    # DISTINGUISHABLE from a validation 4xx: different status code, and no retry_after there.
    rb = client.post("/api/workflows/7/run-badlocale")
    assert rb.status_code == 400 and "retry_after" not in rb.json(), rb.json()
    print("  [H5] a contended launch surfaces an honest retryable 503, distinct from a 400")


def test_commit_with_retry_keep_loaded_avoids_post_commit_select():
    """Fold-in 2: the launch sites read the new row's PK WITHOUT a post-commit
    `session.refresh(row)` — that refresh is a SELECT which, on the launch hot path, could
    itself lose the writer lock and 500 (the R2-F1(a) shape). `keep_loaded=True` suppresses
    the post-commit expiry so the row stays populated (PK readable, NO reload SELECT), and
    `expire_on_commit` is restored afterwards so nothing else in the session is affected."""
    from sqlalchemy import inspect as _sa_inspect

    with Session(engine) as s:
        assert s.expire_on_commit is True, "precondition: default expiry is on"
        p = Project(name="wc-keep-loaded", source_locale="en-US")

        def _stage():
            s.add(p)

        _db.commit_with_retry(s, stage=_stage, keep_loaded=True)
        # The committed row is NOT expired -> reading it triggers no reload SELECT.
        assert _sa_inspect(p).expired is False, (
            "keep_loaded must leave the committed row unexpired (no post-commit SELECT)"
        )
        assert p.id is not None and p.name == "wc-keep-loaded", (
            "the server-assigned PK is readable without a refresh",
            p.id,
        )
        assert s.expire_on_commit is True, (
            "expire_on_commit must be restored so later commits behave normally"
        )
    print("  [H6] keep_loaded leaves the row unexpired (no lock-prone post-commit refresh)")


# --------------------------------------------------------------------------- #
# [I] verify_key expired-reload — R2-F1: the launch 500 whose traceback ran through
#     orm/state.py `_load_expired`. The old usage-stamp committed on the SAME session as
#     the returned key (expiring it) then refresh()'d to reload; under contention the
#     refresh SELECT lost the lock, was swallowed, and the key was returned EXPIRED +
#     detached -> api_identity's `key.user_id` triggered a load on a closed session ->
#     DetachedInstanceError -> 500 on the launch request. The fix expunges the key with
#     attributes INTACT and stamps on a SEPARATE session, so a locked stamp is harmless.
# --------------------------------------------------------------------------- #
def test_verify_key_returns_usable_key_when_usage_stamp_locks(monkeypatch):
    from app.models import User
    from app.services import apikeys
    from app.web import deps

    with Session(engine) as s:
        u = User(email="flood-r2@example.com", active=True, role="admin")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    _key, full = apikeys.create_key(uid, name="flood", scopes="docs")

    # Force the best-effort usage stamp to lose the lock forever (its retry never clears).
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)

    def _always_lock(session, *, stage=None, attempts=_db.WRITE_RETRY_ATTEMPTS):
        if stage is not None:
            stage()
        raise _locked_error("UPDATE apikey SET last_used=? WHERE apikey.id = ...")

    monkeypatch.setattr(_db, "commit_with_retry", _always_lock)

    key = apikeys.verify_key(full)
    assert key is not None, "a valid key must still authenticate when its usage stamp locks"
    # THE REGRESSION: reading key.user_id after return must NOT raise DetachedInstanceError.
    assert key.user_id == uid, ("the returned key carries its loaded attributes", key.user_id)
    assert "docs" in (key.scopes or ""), ("scopes readable on the detached key", key.scopes)

    # And the LAUNCH-request gate resolves the identity — this is exactly where the 500 fired
    # (deps.api_identity -> s.get(User, key.user_id)).
    ident = deps.api_identity(SimpleNamespace(headers={"X-Loc-Token": full}))
    assert ident and ident.get("kind") == "user" and ident.get("actor") == "flood-r2@example.com", (
        "api_identity resolves the caller even when the usage stamp locks",
        ident,
    )
    print("  [I] verify_key returns a usable (non-detached) key even when the usage stamp locks")


# --------------------------------------------------------------------------- #
# [J] AUDIT write path — R3-F1 (volume-flood): audit.log() is written AFTER call_next
#     on EVERY mutating request, and the middleware's `_audit_mutating_request` calls it
#     UNWRAPPED. A lost writer lock used to raise `database is locked` straight up ->
#     a 500 on an otherwise-successful mutating request (two such 500s in the R3 flood).
#     It now RIDES OUT the lock and DEGRADES fail-OPEN: one WARN naming the dropped
#     action, the row dropped, NO exception -> the request proceeds unaudited.
# --------------------------------------------------------------------------- #
def _audit_count():
    from app.models import AuditLog

    with Session(engine) as s:
        return len(s.exec(select(AuditLog)).all())


def _always_lock_cwr(stmt):
    """A commit_with_retry stand-in that STAGES then loses the lock forever (persistent
    contention). Accepts commit_with_retry's full kwarg surface incl. keep_loaded so it
    can stand in for the call commit_or_shed makes."""

    def _fn(session, *, stage=None, attempts=_db.WRITE_RETRY_ATTEMPTS, keep_loaded=False):
        if stage is not None:
            stage()
        raise _locked_error(stmt)

    return _fn


def test_audit_log_happy_path_writes_row_and_chain_verifies():
    from app.services import audit

    settings.set_many({"audit_enabled": "1"})
    before = _audit_count()
    audit.log(actor="ok@example.com", action="POST /api/docs", ip="127.0.0.1")
    assert _audit_count() == before + 1, "an uncontended audit write lands exactly one row"
    ok, checked, bad = audit.verify_chain()
    assert ok, ("the HMAC chain verifies after a normal write", checked, bad)
    print("  [J] audit.log writes a row on the happy path and the chain verifies")


def test_audit_log_degrades_fail_open_under_persistent_lock(monkeypatch):
    from app.services import audit

    settings.set_many({"audit_enabled": "1"})
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)
    logger, handler = _capture("loc_tms.audit")
    prev_level = logger.level
    logger.setLevel(_logging.WARNING)
    before = _audit_count()
    monkeypatch.setattr(_db, "commit_with_retry", _always_lock_cwr("INSERT INTO auditlog ..."))
    raised = False
    try:
        # EXACTLY what the middleware's `_audit_mutating_request` calls, UNWRAPPED — the
        # request-path shape: a mutating request's audit tail must never 500 on a lock.
        audit.log(actor="flood@example.com", action="POST /api/keys", ip="127.0.0.1")
    except Exception:
        raised = True
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
    assert raised is False, "a mutating request's audit tail must NEVER 500 on a lock (fail-open)"
    assert _audit_count() == before, "the shed audit row must not have been written"
    msgs = [r.getMessage() for r in handler.records if r.levelno == _logging.WARNING]
    assert any("DROPPED row for action" in m and "POST /api/keys" in m for m in msgs), (
        "the degrade logs ONE WARN naming the dropped action",
        msgs,
    )
    print("  [J2] audit.log degrades fail-open under a persistent lock (WARN + drop, no 500)")


# --------------------------------------------------------------------------- #
# [J3] AUDIT contention POSTURE is a Setting (maintainer-ratified 2026-08-13): the former
#      audit._AUDIT_FAIL_OPEN module placeholder is now the `audit_fail_open` Setting row
#      (default "1" = fail-open), read via audit.fail_open() and SNAPSHOT at the top of
#      audit.log(). fail-open default proceeds; fail-closed via the setting raises
#      WriteContentionError; and the posture snapshot means a settings read is never issued
#      DURING the degrade (so a locked DB can't turn the posture decision into a 500).
# --------------------------------------------------------------------------- #
def _guarded_settings(monkeypatch):
    """Wrap settings.get so that, once `state['locked']` flips True, ANY settings read
    raises a SQLITE_BUSY error — the locked-DB-during-degrade scenario. Returns the shared
    `state` dict; the caller flips state['locked'] at the shed boundary."""
    from app.services import settings as _s

    real_get = _s.get
    state = {"locked": False, "get_calls": 0}

    def guarded_get(key, default=None):
        state["get_calls"] += 1
        if state["locked"]:
            raise _locked_error("SELECT value FROM setting WHERE key = ...")
        return real_get(key, default)

    monkeypatch.setattr(_s, "get", guarded_get)
    return state


def _shed_then_lock(state):
    """A commit_or_shed stand-in: STAGE the row, flip the shared lock so any later settings
    read would raise, then SHED (return False). Models a writer-lock flood in which reading a
    setting is now itself lock-exposed."""

    def _fn(session, *, stage=None, attempts=_db.WRITE_RETRY_ATTEMPTS, keep_loaded=False):
        if stage is not None:
            stage()
        state["locked"] = True
        return False

    return _fn


def test_audit_fail_open_setting_defaults_open():
    from app.services import audit

    # UNSET row -> DEFAULTS["audit_fail_open"] == "1" -> the ratified fail-OPEN default.
    assert settings.get("audit_fail_open") == "1", "the shipped default is fail-OPEN"
    assert audit.fail_open() is True, "fail_open() reads the default as OPEN"
    try:
        settings.set_many({"audit_fail_open": "0"})
        assert audit.fail_open() is False, "the setting flips the posture to fail-CLOSED"
    finally:
        settings.set_many({"audit_fail_open": "1"})  # restore the default for later tests
    assert audit.fail_open() is True
    print("  [J3] audit.fail_open() reads the audit_fail_open Setting (default OPEN, flippable)")


def test_audit_fail_closed_via_setting_raises(monkeypatch):
    from app.services import audit

    settings.set_many({"audit_enabled": "1", "audit_fail_open": "0"})
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)
    logger, handler = _capture("loc_tms.audit")
    prev_level = logger.level
    logger.setLevel(_logging.WARNING)
    before = _audit_count()
    # Lock settings reads at the shed boundary: if the degrade issued a settings read it would
    # raise OperationalError; seeing the honest WriteContentionError instead PROVES the
    # posture came from the snapshot taken at the top of log(), not a degrade-time DB read.
    state = _guarded_settings(monkeypatch)
    monkeypatch.setattr(_db, "commit_or_shed", _shed_then_lock(state))
    raised = None
    try:
        audit.log(actor="flood@example.com", action="POST /api/keys", ip="127.0.0.1")
    except _db.WriteContentionError as exc:
        raised = exc
    except OperationalError as exc:
        raised = exc  # would mean a settings read fired DURING the degrade (the bug)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        state["locked"] = False
        settings.set_many({"audit_fail_open": "1"})  # restore the default posture
    assert isinstance(raised, _db.WriteContentionError), (
        "fail-CLOSED via audit_fail_open=0 raises WriteContentionError; a settings read "
        "during the degrade would have raised OperationalError instead",
        raised,
    )
    assert _audit_count() == before, "the shed audit row must not have been written"
    msgs = [r.getMessage() for r in handler.records if r.levelno == _logging.WARNING]
    assert any(
        "DROPPED row for action" in m and "POST /api/keys" in m and "fail-CLOSED" in m for m in msgs
    ), ("the degrade still logs ONE WARN, naming fail-CLOSED", msgs)
    print(
        "  [J3b] fail-CLOSED via the setting raises WriteContentionError (snapshot, not a "
        "degrade-time DB read)"
    )


def test_audit_fail_open_snapshot_survives_locked_setting_during_degrade(monkeypatch):
    from app.services import audit

    settings.set_many({"audit_enabled": "1", "audit_fail_open": "1"})
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)
    logger, handler = _capture("loc_tms.audit")
    prev_level = logger.level
    logger.setLevel(_logging.WARNING)
    before = _audit_count()
    state = _guarded_settings(monkeypatch)
    monkeypatch.setattr(_db, "commit_or_shed", _shed_then_lock(state))
    raised = None
    try:
        # If the degrade read the posture from settings (not the snapshot), the locked
        # settings.get would raise here and the mutating request would 500 — the exact
        # regression the snapshot prevents.
        audit.log(actor="flood@example.com", action="POST /api/docs", ip="127.0.0.1")
    except Exception as exc:
        raised = exc
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        state["locked"] = False
    assert raised is None, (
        "fail-OPEN degrade must not raise, and must not issue a settings read during the "
        "degrade (a locked-DB read would raise) — the snapshot is consulted instead",
        raised,
    )
    assert _audit_count() == before, "the shed row was not written"
    msgs = [r.getMessage() for r in handler.records if r.levelno == _logging.WARNING]
    assert any(
        "DROPPED row for action" in m and "POST /api/docs" in m and "fail-OPEN" in m for m in msgs
    ), ("the degrade logs ONE fail-OPEN WARN", msgs)
    print("  [J3c] fail-OPEN posture snapshot survives a locked-DB settings read mid-degrade")


def test_audit_fail_open_snapshot_read_locked_with_force_degrades_open(monkeypatch):
    """The SNAPSHOT read itself can be lock-exposed: the settings cache is PER-KEY, so a
    forced audit row (force=True SKIPS enabled(), making fail_open() the ONLY settings read)
    with a COLD `audit_fail_open` key against a LOCKED DB would raise a raw OperationalError
    out of audit.log() BEFORE any write -> a 500 the old module flag could not produce. The
    guarded snapshot must DEFAULT to fail-open and never raise."""
    from app.services import audit

    settings.set_many({"audit_fail_open": "1"})  # value irrelevant; the read itself will raise
    monkeypatch.setattr(_db.time, "sleep", lambda d: None)
    logger, handler = _capture("loc_tms.audit")
    prev_level = logger.level
    logger.setLevel(_logging.WARNING)
    before = _audit_count()
    state = _guarded_settings(monkeypatch)
    monkeypatch.setattr(_db, "commit_or_shed", _shed_then_lock(state))
    monkeypatch.setattr(audit, "_POSTURE_WARNED", False)  # reset the latch for a clean assert
    # Lock the settings seam FROM THE START so the SNAPSHOT read at the top of log() raises.
    state["locked"] = True
    raised = None
    try:
        # A FORCED verb (backoffice impersonation / org.* / eval_promote) — enabled() is
        # skipped, so the snapshot fail_open() is the only settings read on this path.
        audit.log(
            actor="admin@example.com", action="org.impersonate.start", ip="127.0.0.1", force=True
        )
    except Exception as exc:
        raised = exc
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        state["locked"] = False
    assert raised is None, (
        "a locked/cold settings read at SNAPSHOT time (force=True) must default to fail-open, "
        "never raise out of the audit tail",
        raised,
    )
    # The snapshot read really fired — a future refactor deleting it can't silently pass here.
    assert state["get_calls"] >= 1, "the snapshot must actually invoke settings.get"
    assert _audit_count() == before, "the shed forced row was not written"
    msgs = [r.getMessage() for r in handler.records if r.levelno == _logging.WARNING]
    assert any(
        "DROPPED row for action" in m and "org.impersonate.start" in m and "fail-OPEN" in m
        for m in msgs
    ), ("a failed snapshot read defaults to the fail-OPEN posture", msgs)
    # The once-per-process latch made the NON-transient posture-read failure visible.
    assert any("audit_fail_open posture" in m and "fail-OPEN" in m for m in msgs), (
        "a failed posture read logs the once-per-process WARN",
        msgs,
    )
    print(
        "  [J3d] a locked settings read at snapshot time (force=True) degrades fail-open, no raise"
    )


# --------------------------------------------------------------------------- #
# a tiny monkeypatch shim so the suite runs standalone (no pytest required),
# mirroring the other verify_*.py scripts that are driven by `python <file>`.
# --------------------------------------------------------------------------- #
class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value=None):
        if value is None:  # setattr(obj_attr_path?, val) form not used here
            raise ValueError("use setattr(obj, name, value)")
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            try:  # noqa: SIM105
                setattr(target, name, old)
            except Exception:
                pass
        self._undo.clear()


def _run(fn, needs_mp):
    if needs_mp:
        mp = _MonkeyPatch()
        try:
            fn(mp)
        finally:
            mp.undo()
    else:
        fn()


def main():
    init_db()
    settings.set_many({"spend_cap_usd": "0"})
    test_is_locked_error_classifies()
    _run(test_commit_with_retry_succeeds_after_transient_lock, True)
    _run(test_commit_with_retry_exhausts_and_reraises, True)
    _run(test_lock_crash_surfaces_honest_retryable_error, True)
    _run(test_non_lock_crash_records_bounded_reason, True)
    _run(test_happy_path_completes_and_uses_retry_seam, True)
    _run(test_cancel_during_retry_is_not_clobbered, True)
    test_contention_note_outranks_benign_step_note()
    test_wal_and_busy_timeout_installed()
    # [G] prompt-tap write paths (R1-F1 volume-flood): retry + degrade, never raise.
    _run(test_commit_tap_write_rides_out_transient_lock, True)
    _run(test_commit_tap_write_degrades_on_persistent_lock, True)
    _run(test_enrich_never_raises_under_persistent_lock, True)
    _run(test_enrich_happy_path_writes_detail_via_retry_seam, True)
    # [H] launch write paths (R2-F1 volume-flood): retry -> honest 503, never a 500.
    _run(test_commit_launch_rides_out_transient_lock, True)
    _run(test_commit_launch_exhaustion_raises_write_contention_error, True)
    _run(test_start_run_launch_rides_out_then_exhausts, True)
    test_launch_contention_surfaces_honest_503_over_http()
    test_commit_with_retry_keep_loaded_avoids_post_commit_select()
    # [I] verify_key expired-reload regression (the _load_expired launch 500).
    _run(test_verify_key_returns_usable_key_when_usage_stamp_locks, True)
    # [J] audit write path (R3-F1 volume-flood): retry + fail-open degrade, never a 500.
    test_audit_log_happy_path_writes_row_and_chain_verifies()
    _run(test_audit_log_degrades_fail_open_under_persistent_lock, True)
    # [J3] the contention posture is now the audit_fail_open Setting (maintainer-ratified),
    # snapshotted at the top of log() so the degrade never issues a lock-exposed settings read.
    test_audit_fail_open_setting_defaults_open()
    _run(test_audit_fail_closed_via_setting_raises, True)
    _run(test_audit_fail_open_snapshot_survives_locked_setting_during_degrade, True)
    _run(test_audit_fail_open_snapshot_read_locked_with_force_degrades_open, True)
    print("WRITE CONTENTION OK")


if __name__ == "__main__":
    main()
