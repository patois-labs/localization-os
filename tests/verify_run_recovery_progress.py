"""GitHub #44 (a stuck/failed plain run silently eats a locale) + #47 (the key grid
shows nothing while runs are in flight).

THE PROD STORY BOTH ISSUES COME FROM. Translate -> all locales on project 12 reported
"Started 3 run(s) into every locale." es-419 and de-DE completed; fr-FR run #74 ran
twelve minutes, FAILED at its fix step, wrote nothing (by design -- write-back is
all-or-nothing) and NO surface ever said so. Two independent mechanisms:

  A. a plain run whose thread a restart killed stays RUNNING forever, and
     `orchestrator.start_run`'s double-submit guard then re-attaches EVERY later
     attempt at that (workflow, document, locale) to the corpse -- so the locale
     reports "started" and translates nothing, permanently; and
  B. the key grid has no live channel at all, so a failed run is indistinguishable
     from "never started" for as long as anyone cares to look.

GROUPS
  [A] STARTUP RECOVERY (`orchestrator.recover_interrupted_runs`). A planted RUNNING
      plain run is FAILED with the note; a PAUSED one (durable human state) and a
      BATCH MEMBER (the other healer's job) are both left untouched; and after the
      sweep a re-launch creates a NEW run that actually completes and writes the
      locale's values -- the end-to-end proof that the corpse was really unblocking.
  [B] THE HONEST RE-ATTACH + THE STALENESS FLOOR. With a FRESH RUNNING run planted
      for one locale, Translate -> all names that locale ("fr-FR is already running
      (run #N)"), does NOT count it as started, and still runs the other locales.
      With a STALE one (started_at 7h ago), no re-attach happens at all: the corpse
      is failed with the note and a brand-new run starts.
  [C] THE GRID HAND-OFF (#47). The translate POST parks the launched run ids for the
      grid, the grid renders the progress strip with the data-* attributes the widget
      reads, and CONSUMES the cookie so the post-completion auto-reload cannot loop.
      A forged cookie naming a foreign run renders nothing.
  [D] THE FAILURE IS ON THE WIRE. The `closed` event the widget consumes carries the
      run's real status and, for a FAILED run, the step error text -- and for a run
      failed OUTSIDE the step machinery (the restart sweep), the run's own note.
      This is the payload that makes a silent failure impossible.
  [F] WHAT THE SWEEPERS REFUSE TO TOUCH (the adversarial-review round). A sweep is a
      DB write and cannot stop a thread, so a false positive is never cosmetic: it is
      two runs racing writes over one segment set, a batch left un-finalizable, or a
      live CLI run failed out from under itself. Each cell is one such case -- a live
      execution thread, a run swept mid-flight trying to resurrect itself, a run
      resumed after an overnight pause, a long run judged on activity rather than row
      age, a batch member, a second process's run inside the activity window, a
      month-old PAUSED run -- plus the honest-notice and hostile-cookie halves.
  [N1] THE ACTIVITY SIGNAL IS WRITTEN BY PRODUCTION. `_last_activity_at` reads
      StepRun.started_at / finished_at, and nothing in orchestrator.py used to set
      either -- so every activity-based guard above was a no-op on real data while
      this suite stayed green on hand-stamped fixtures. These cells drive REAL runs
      and read what the orchestrator actually persisted: a 3-step run stamps every
      row, a 9h-old row with seconds-old REAL commits is not stale, and the boot
      healer spares it.
  [G] THE WIDGET'S OWN DECISIONS, driven through the REAL app/static/keysrun.js in a
      minimal DOM (tests/keysrun_probe.js, run under `node` when it is available):
      when the grid may be reloaded out from under the operator, whether a FAILED or
      PAUSED run survives on screen, what happens when the server's observer window
      expires, that replayed steps do not inflate the counter, and the connection
      budget. Plus the run view's escaping of every provider-supplied field.
  [E] STREAM AUTH. A project member may open /runs/stream for a grid-launched run;
      a user outside the project's ring is refused. Asserted with the login wall ON
      (with it off every scope predicate returns None for everyone and the cell is
      decoration).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env, provider
keys blanked, MockProvider). ASCII-only output. Final line: "RUN RECOVERY PROGRESS OK".
"""

import os
import sys
import time
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_runrecov_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    ProjectMember,
    ProjectType,
    Role,
    RunStatus,
    Segment,
    StepRun,
    StepType,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    content,
    orchestrator,
    security,
    settings,
)
from app.services import (
    keys as keys_svc,
)
from app.services import workflows as wf_service
from tests._csrf import csrf_for

SRC = "en-US"
PW = "runrecov-pw-123456"
_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _switch(on: bool) -> None:
    settings.set_many({"key_projects_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _customer():
    with Session(engine) as s:
        c = Customer(name="RunRecovCust %d" % _seq())  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _key_project(targets="de-DE, fr-FR"):
    cust = _customer()
    with Session(engine) as s:
        p = content.project_create(
            s,
            "RunRecov %d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            target_locales=targets,
            customer_id=str(cust),
            default_provider="mock",
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _add_key(pid, name, base="Hello there."):
    from app.models import Project

    with Session(engine) as s:
        return keys_svc.create_key(
            s, s.get(Project, pid), name, base_value=base, actor="runrecov-fixture"
        ).id


def _workflow(target="de-DE", steps=1):
    with Session(engine) as s:
        w = Workflow(
            name="RunRecovWF %d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale=SRC,
            target_locale=target,  # noqa: RUF100, UP031
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        for i in range(steps):
            wf_service.add_step(
                s,
                w.id,
                name="Translate %d" % (i + 1),  # noqa: UP031
                step_type=StepType.LLM_AGENT,
                provider="mock",
                prompt_text="Translate to {target_locale}: {text}",
                chunk_policy="none",
                tm_examples=False,
            )
        return w.id


def _ns_doc(pid):
    from app.models import Project

    with Session(engine) as s:
        docs = keys_svc.translatable_documents(s, s.get(Project, pid))
        assert len(docs) == 1, docs
        return docs[0].id


def _plant_run(
    doc_id, wf_id, locale, status=RunStatus.RUNNING, age_hours=0.0, batch_id=None, finished=False
):
    """A WorkflowRun row in whatever state the cell needs -- the corpse a killed
    daemon thread leaves behind, planted directly because there is no honest way to
    kill a thread mid-run from inside the same process."""
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_id,
            target_locale=locale,
            status=status,
            batch_id=batch_id,
            created_by="planted",
            started_at=datetime.utcnow() - timedelta(hours=age_hours),
            finished_at=datetime.utcnow() if finished else None,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id


def _plant_historical_steprun(run_id, wf_id, minutes_ago, status=RunStatus.COMPLETED, error=None):
    """A StepRun back-dated by `minutes_ago` -- a HISTORICAL row, hand-stamped on
    purpose, for cells that need a run whose last sign of life is in the past.

    Deliberately NOT how a live run's activity is simulated: see
    `_commit_real_steprun`, which goes through the production constructor. Inventing
    timestamps everywhere was how the activity signal came to be tested against a
    shape production never wrote -- nothing in orchestrator.py set these columns, so
    the staleness floor and the boot healer's grace window were inert on real data
    while this suite stayed green."""
    when = datetime.utcnow() - timedelta(minutes=minutes_ago)
    with Session(engine) as s:
        step = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).first()
        sr = StepRun(
            workflow_run_id=run_id,
            step_id=step.id,
            status=status,
            error=error,
            started_at=when,
            finished_at=when,
        )
        s.add(sr)
        s.commit()
        s.refresh(sr)
        return sr.id


def _real_run(pid, wf_id, locale="de-DE"):
    """Drive a REAL run through the orchestrator to completion and return its id.

    Its StepRun rows carry exactly the timestamps PRODUCTION stamps -- this suite
    invents none. That is the whole point of the cells below: if orchestrator.py
    stops stamping those columns, the activity-based staleness floor and the boot
    healer's grace window silently become no-ops, and only a fixture that goes
    through the real write path notices."""
    doc_id = _ns_doc(pid)
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wf_id, doc_id, locale)
    status = _await_terminal(rid)
    if status != RunStatus.COMPLETED:
        _fail(f"fixture run {rid} ended {status!r}, expected completed")
    return rid


def _step_times(run_id):
    """[(status, started_at, finished_at)] for a run's StepRuns, id-ascending."""
    with Session(engine) as s:
        rows = s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()
        return [(r.status, r.started_at, r.finished_at) for r in rows]


def _age_run_row(run_id, hours, status=RunStatus.RUNNING):
    """Back-date the WorkflowRun ROW and put it back into `status`, leaving its
    StepRun timestamps exactly as production wrote them.

    This is the shape both sweepers have to get right: an OLD row whose actual work
    happened moments ago (a genuinely long run, or one resumed after a pause)."""
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        run.started_at = datetime.utcnow() - timedelta(hours=hours)
        run.status = status
        run.finished_at = None
        s.add(run)
        s.commit()


def _run_row(rid):
    with Session(engine) as s:
        return s.get(WorkflowRun, rid)


def _retire(*run_ids):
    """Park a cell's deliberately-still-RUNNING planted rows in a terminal state.

    `recover_interrupted_runs` is GLOBAL over the DB, so a cell that leaves a corpse
    behind changes the healed COUNT a later cell asserts on. Cleaning up keeps each
    count assertion about the row the cell actually planted."""
    with Session(engine) as s:
        for rid in run_ids:
            run = s.get(WorkflowRun, rid)
            if run is not None and run.status in (RunStatus.RUNNING, RunStatus.PENDING):
                run.status = RunStatus.FAILED
                run.finished_at = datetime.utcnow()
                s.add(run)
        s.commit()


def _await_terminal(rid, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = _run_row(rid)
        if run is not None and run.status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.PAUSED,
        ):
            return run.status
        time.sleep(0.05)
    _fail(f"run {rid} never reached a terminal state")


def _await_all_terminal(pid, timeout=120, planted=()):
    """Drain every run over this project's documents (keeps a cell from leaking a
    worker thread into the next one).

    `planted` names rows no worker thread will ever execute: rows the cell planted itself
    with _plant_run, and runs it queued with start_scheduler=False. They never move, so
    waiting on them only burned the full timeout (three cells spent 120 s each here, 6 of
    the suite's 6 minutes; test speed, 2026-10-08). Every run a real thread executes is
    still drained."""
    doc_ids = _doc_ids(pid)
    skip = set(planted)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            rows = s.exec(select(WorkflowRun).where(WorkflowRun.document_id.in_(doc_ids))).all()
        if all(
            r.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PAUSED) or r.id in skip
            for r in rows
        ):
            return
        time.sleep(0.05)


def _doc_ids(pid):
    from app.models import Project

    with Session(engine) as s:
        return [d.id for d in keys_svc.translatable_documents(s, s.get(Project, pid))]


def _targets(doc_id, locale):
    with Session(engine) as s:
        rows = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).all()
        return [(r.order_index, r.target_text) for r in rows]


def _runs_for(pid, locale):
    with Session(engine) as s:
        return s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id.in_(_doc_ids(pid)), WorkflowRun.target_locale == locale)
            .order_by(WorkflowRun.id)
        ).all()


def _client():
    c = TestClient(app)
    csrf_for(c)
    return c


def _translate(client, pid, wf_id, locale):
    return client.post(
        "/projects/%d/keys/translate" % pid,  # noqa: UP031
        data={"workflow_id": str(wf_id), "locale": locale},
        follow_redirects=False,
    )


def _notice(client, resp) -> str:
    """The flash the OPERATOR actually reads, isolated from the rest of the grid.

    Scoped to the `<p class="ok">` element on purpose: asserting against the whole
    page made a cell pass/fail on unrelated body copy (the Key settings card's help
    text contains the phrase "every locale", which is nothing to do with the notice)."""
    html = client.get(resp.headers["location"]).text
    marker = '<p class="ok">'
    if marker not in html:
        return ""
    body = html.split(marker, 1)[1]
    return body.split("</p>", 1)[0].strip()


# ============================================================================
# [A] startup recovery
# ============================================================================
def test_a_recovery_fails_the_orphan_and_unblocks_the_locale():
    """The whole #44 chain in one cell: plant the corpse, prove the documented
    healers do NOT touch it before the fix's own healer runs, sweep, then RE-LAUNCH
    and watch a NEW run actually translate the locale."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "recov::one", base="Save your changes.")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    # Aged past RECOVERY_ACTIVITY_GRACE with no StepRun at all -- what a killed
    # daemon thread genuinely leaves behind, and what the healer is allowed to fail.
    corpse = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.RUNNING, age_hours=1)

    # The two PRE-EXISTING healers are documented not to touch a plain run; assert it
    # rather than believing the comment, so this cell keeps meaning something if one
    # of them ever grows a claim over `batch_id IS NULL`.
    from app.services import batch_runs, jobs

    jobs.recover_interrupted()
    batch_runs.recover_interrupted_batches(at_boot=True)
    if _run_row(corpse).status != RunStatus.RUNNING:
        _fail("the job/batch healers touched a plain run; this cell's premise is gone")

    healed = orchestrator.recover_interrupted_runs()
    if healed != 1:
        _fail(f"recover_interrupted_runs healed {healed!r} runs, expected 1")
    row = _run_row(corpse)
    if row.status != RunStatus.FAILED:
        _fail(f"the orphaned RUNNING run was not failed (status {row.status!r})")
    if row.finished_at is None:
        _fail("a healed run must be stamped finished_at, or it reads in-flight forever")
    if orchestrator.RESTART_NOTE not in (row.notes or ""):
        _fail(f"a healed run must carry the reason it died, got notes={row.notes!r}")

    # ...and the locale is genuinely unblocked: a fresh launch is a NEW run that
    # completes and writes values. Before the fix this POST returned the corpse's id.
    client = _client()
    r = _translate(client, pid, wf_id, "de-DE")
    if r.status_code != 303:
        _fail(f"translate answered {r.status_code}: {r.text[:200]}")
    loc = r.headers.get("location", "")
    if "/runs/" not in loc:
        _fail(f"a single-locale translate must redirect to the run view, got {loc!r}")
    new_id = int(loc.split("/runs/")[1].split("?")[0])
    if new_id == corpse:
        _fail("the re-launch re-attached to the healed corpse (run %d)" % corpse)  # noqa: UP031
    status = _await_terminal(new_id)
    if status != RunStatus.COMPLETED:
        _fail(f"the fresh run ended {status!r}, expected completed")
    targets = _targets(doc_id, "de-DE")
    if not targets or not all(t for _i, t in targets):
        _fail(f"the fresh run completed but wrote no target text: {targets!r}")
    print(
        "  [A] a planted RUNNING plain run is FAILED with the restart note (the "
        "job/batch healers leave it alone), and the locale then translates on a "
        "NEW run OK"
    )


def test_a_paused_and_batch_members_are_left_alone():
    """PAUSED is durable HUMAN state -- a reviewer may come back to it days later --
    and a batch member belongs to the OTHER healer. Sweeping either would be a
    regression, so both are planted alongside a genuine orphan."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "recov::paused")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    with Session(engine) as s:
        batch = WorkflowBatch(
            workflow_id=wf_id, document_id=doc_id, requested_locales=["de-DE"], status="running"
        )
        s.add(batch)
        s.commit()
        s.refresh(batch)
        batch_id = batch.id
    paused = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.PAUSED)
    member = _plant_run(doc_id, wf_id, "fr-FR", status=RunStatus.RUNNING, batch_id=batch_id)
    orphan = _plant_run(doc_id, wf_id, "it-IT", status=RunStatus.RUNNING, age_hours=1)

    healed = orchestrator.recover_interrupted_runs()
    if healed != 1:
        _fail(f"expected exactly the 1 orphan healed, got {healed!r}")
    if _run_row(paused).status != RunStatus.PAUSED:
        _fail("a PAUSED plain run was swept -- durable human review state destroyed")
    if _run_row(paused).notes:
        _fail("a PAUSED run must not even be annotated by the sweep")
    if _run_row(member).status != RunStatus.RUNNING:
        _fail("a BATCH MEMBER was swept by the plain-run healer (double heal)")
    if _run_row(orphan).status != RunStatus.FAILED:
        _fail("the genuine orphan was not healed")
    print(
        "  [A] the sweep is exact: PAUSED (human state) and batch members "
        "(the other healer's rows) are untouched; only the plain orphan fails OK"
    )


# ============================================================================
# [B] honest re-attach + staleness floor
# ============================================================================
def test_b_fresh_running_run_is_reported_not_counted_as_started():
    pid = _key_project(targets="de-DE, fr-FR")
    _add_key(pid, "reattach::one")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    blocked = _plant_run(doc_id, wf_id, "fr-FR", status=RunStatus.RUNNING)

    client = _client()
    r = _translate(client, pid, wf_id, "all")
    if r.status_code != 303:
        _fail(f"translate-all answered {r.status_code}")
    # Read the notice as the OPERATOR sees it (the grid renders `ok`), not as an
    # encoded query string -- the claim is about what the human is told.
    notice = _notice(client, r)
    if "Started 2 run(s)" in notice:
        _fail(
            "a re-attached run is still being counted as started (the exact #44 "
            "defect: 'Started N run(s)' over a locale that will never translate)"
        )
    if "Started 1 run(s)" not in notice:
        _fail("the notice must report the ONE immediately admitted run")
    if "every locale" in notice:
        _fail(
            "only one of two locales started, so the notice must not claim the "
            "whole fan-out (review F8d)"
        )
    if ("fr-FR is already running (run #%d)." % blocked) not in notice:  # noqa: UP031
        _fail(
            "the notice must NAME the already-running locale and its run id "  # noqa: UP031
            "(expected 'fr-FR is already running (run #%d).')" % blocked
        )
    de = _runs_for(pid, "de-DE")
    fr = _runs_for(pid, "fr-FR")
    if len(de) != 1:
        _fail("de-DE should have exactly one fresh run, got %r" % [x.id for x in de])
    if [x.id for x in fr] != [blocked]:
        _fail(
            "fr-FR must NOT have spawned a second concurrent run over the same "
            "segment set, got %r" % [x.id for x in fr]
        )
    _await_all_terminal(pid, planted={blocked})
    print(
        "  [B] a fresh RUNNING run is reported by name ('fr-FR is already running "  # noqa: UP031
        "(run #%d)'), is not counted as started, and the other locale still runs OK" % blocked
    )


def test_b_stale_running_run_is_swept_not_reattached():
    # Two targets so the fan-out takes the NOTICE path (a single launched run with
    # nothing else to report redirects straight to the run view instead).
    pid = _key_project(targets="de-DE, fr-FR")
    _add_key(pid, "stale::one", base="Save your changes.")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    stale = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.RUNNING, age_hours=7)

    client = _client()
    r = _translate(client, pid, wf_id, "all")
    if r.status_code != 303:
        _fail(f"translate answered {r.status_code}")
    notice = _notice(client, r)
    if "already running" in notice:
        _fail("a 7h-old RUNNING corpse was treated as a valid re-attach target")
    row = _run_row(stale)
    if row.status != RunStatus.FAILED:
        _fail(f"the stale run should have been failed, status {row.status!r}")
    fresh = [x for x in _runs_for(pid, "de-DE") if x.id != stale]
    if len(fresh) != 1:
        _fail("exactly one fresh run should have started, got %r" % [x.id for x in fresh])
    # F8a/F8b: the operator is TOLD a corpse was superseded and by which run, and the
    # swept row carries a note that says supersession -- not "a server restarted",
    # which nothing did.
    expect = "de-DE: stale run #%d superseded, started as #%d." % (stale, fresh[0].id)  # noqa: UP031
    if expect not in notice:
        _fail(f"the notice must own up to the supersession (expected {expect!r})")
    if orchestrator.RESTART_NOTE in (row.notes or ""):
        _fail(f"the staleness sweep must NOT claim a server restart, notes={row.notes!r}")
    if (orchestrator.STALE_NOTE % fresh[0].id) not in (row.notes or ""):
        _fail(f"the swept row must carry STALE_NOTE naming its successor, notes={row.notes!r}")
    if _await_terminal(fresh[0].id) != RunStatus.COMPLETED:
        _fail("the fresh run did not complete")
    if not all(t for _i, t in _targets(doc_id, "de-DE")):
        _fail("the fresh run wrote no targets -- the locale is still blocked")
    _await_all_terminal(pid)
    print(
        f"  [B] a RUNNING run older than the {orchestrator.STALE_RUN_AFTER} staleness floor is failed with the "  # noqa: E501
        "note and a fresh run translates the locale OK"
    )


def test_b_detail_out_parameter_distinguishes_the_two():
    """The signal itself, at the seam, without a route in the way."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "detail::one")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    first, second = {}, {}
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wf_id, doc_id, "de-DE", detail=first)
    if (first.get("run_id"), first.get("reattached")) != (rid, False):
        _fail(f"a FRESH launch must report reattached=False, got {first!r}")
    if "superseded" in first:
        _fail(f"nothing was superseded, so the key must be absent, got {first!r}")
    with Session(engine) as s:
        rid2 = orchestrator.start_run(s, wf_id, doc_id, "de-DE", detail=second)
    if rid2 == rid and second.get("reattached") is not True:
        _fail(f"a re-attach must report reattached=True, got {second!r}")
    if rid2 != rid and second.get("reattached") is not False:
        _fail(
            "the first run finished before the second launch; the second is fresh "
            f"and must say so, got {second!r}"
        )
    _await_all_terminal(pid)
    print(
        "  [B] start_run's `detail` out-parameter reports run_id + reattached, "
        "and the return value is unchanged OK"
    )


# ============================================================================
# [213] CROSS-WORKFLOW OVERLAP (GitHub #213 part 2). While ANY run is in flight on
# a (document, locale), a launch of a DIFFERENT workflow there is REFUSED (the prod
# story: runs 237/wf 65 and 239/wf 72 on doc 159 ja-JP in the same second, their
# write-backs interleaving over one segment set). The same-workflow re-attach is
# unchanged; a different LOCALE still starts; a terminal rival releases the door.
# ============================================================================
def test_213_a_different_workflow_is_refused_while_a_run_is_in_flight():
    pid = _key_project(targets="de-DE, fr-FR")
    _add_key(pid, "overlap::one")
    doc_id = _ns_doc(pid)
    wf_a = _workflow("de-DE")
    wf_b = _workflow("de-DE")
    with Session(engine) as s:
        wf_a_name = s.get(Workflow, wf_a).name
    planted = set()
    for status in (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED):
        held = _plant_run(doc_id, wf_a, "de-DE", status=status)
        planted.add(held)
        try:
            with Session(engine) as s:
                orchestrator.start_run(s, wf_b, doc_id, "de-DE")
        except orchestrator.RunConflictError as exc:
            if exc.code != "run_conflict" or exc.run_id != held:
                _fail(
                    f"the refusal over a {status} rival must be run_conflict naming it, "
                    f"got code={exc.code!r} run_id={exc.run_id!r}"
                )
            if f"run #{held}" not in str(exc) or wf_a_name not in str(exc):
                _fail(f"the refusal must NAME the running run and workflow: {exc}")
            if "cancel-run" not in str(exc):
                _fail(f"the refusal must say how to cancel: {exc}")
        else:
            _fail(
                f"a different workflow LAUNCHED over a {status} run on the same "
                "document+locale -- #213 part 2 is not enforced"
            )
        # A DIFFERENT LOCALE of the same document still starts (the guard is
        # per-locale, exactly like the re-attach guard it sits beside).
        with Session(engine) as s:
            rid = orchestrator.start_run(s, wf_b, doc_id, "fr-FR")
        if _await_terminal(rid) != RunStatus.COMPLETED:
            _fail(f"a different locale must still run while de-DE is held ({status})")
        if _run_row(held).status != status:
            _fail(f"the refused launch must leave the {status} rival row alone")
    _await_all_terminal(pid, planted=planted)
    print(
        "  [213] a DIFFERENT workflow on a busy (document, locale) is refused for "
        "PENDING/RUNNING/PAUSED rivals naming run+workflow+cancel; another locale runs OK"
    )


def test_213_same_workflow_still_reattaches_and_a_terminal_rival_releases():
    pid = _key_project(targets="de-DE")
    _add_key(pid, "overlap::two")
    doc_id = _ns_doc(pid)
    wf_a = _workflow("de-DE")
    wf_b = _workflow("de-DE")
    held = _plant_run(doc_id, wf_a, "de-DE", status=RunStatus.RUNNING)
    # The genuine double-submit stays EXACTLY as it was: same workflow re-attaches.
    info: dict = {}
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wf_a, doc_id, "de-DE", detail=info)
    if rid != held or info.get("reattached") is not True:
        _fail(f"the SAME workflow must still re-attach, got run {rid} detail {info!r}")
    # ...and once the rival is CANCELLED (terminal), the second workflow starts.
    with Session(engine) as s:
        row = s.get(WorkflowRun, held)
        row.status = RunStatus.CANCELLED
        row.finished_at = datetime.utcnow()
        s.add(row)
        s.commit()
    with Session(engine) as s:
        rid2 = orchestrator.start_run(s, wf_b, doc_id, "de-DE")
    if rid2 == held:
        _fail("a different workflow re-attached to a terminal rival instead of starting")
    if _await_terminal(rid2) != RunStatus.COMPLETED:
        _fail("the second workflow did not run after the rival cancelled")
    print(
        "  [213] same workflow still RE-ATTACHES (unchanged); after the rival cancels "
        "the second workflow starts and completes OK"
    )


def test_213_a_stale_rival_corpse_does_not_hold_the_door():
    pid = _key_project(targets="de-DE")
    _add_key(pid, "overlap::three")
    doc_id = _ns_doc(pid)
    wf_a = _workflow("de-DE")
    wf_b = _workflow("de-DE")
    stale = _plant_run(doc_id, wf_a, "de-DE", status=RunStatus.RUNNING, age_hours=7)
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wf_b, doc_id, "de-DE")
    if _await_terminal(rid) != RunStatus.COMPLETED:
        _fail("a 7h-old rival corpse must not block a different workflow forever")
    if _run_row(stale).status != RunStatus.RUNNING:
        _fail(
            "another workflow's launch must not sweep the rival's row -- "
            "the staleness floor supersedes it on ITS OWN launch path"
        )
    print(
        "  [213] a stale RUNNING rival (no thread, past the floor) is neither a "
        "conflict nor swept by the other workflow's launch OK"
    )


def test_213_queue_runs_reports_the_conflicting_pair_and_runs_the_rest():
    pid = _key_project(targets="de-DE, fr-FR")
    _add_key(pid, "overlap::four")
    doc_id = _ns_doc(pid)
    wf_a = _workflow("de-DE")
    wf_b = _workflow("de-DE")
    held = _plant_run(doc_id, wf_a, "de-DE", status=RunStatus.RUNNING)
    with Session(engine) as s:
        out = orchestrator.queue_runs(
            s,
            [(wf_b, doc_id, "de-DE", "doc"), (wf_b, doc_id, "fr-FR", "doc")],
            created_by="213-fixture",
            start_scheduler=False,
        )
    if out["attached"] or out["failed"]:
        _fail(f"nothing attaches or fails here, got {out!r}")
    if [r["locale"] for r in out["queued"]] != ["fr-FR"]:
        _fail(f"the non-conflicting pair must queue, got {out['queued']!r}")
    if len(out["conflicts"]) != 1:
        _fail(f"the conflicting pair must be reported, got {out['conflicts']!r}")
    c = out["conflicts"][0]
    if c["locale"] != "de-DE" or c.get("code") != "run_conflict" or c.get("run_id") != held:
        _fail(f"the conflict row must name the busy locale and running run, got {c!r}")
    if f"run #{held}" not in c["reason"]:
        _fail(f"the conflict reason must carry the standard sentence, got {c!r}")
    _await_all_terminal(pid, planted={held} | {r["run_id"] for r in out["queued"]})
    print(
        "  [213] queue_runs (the keys fan-out) reports the conflicting pair with "
        "run_conflict + the running run and queues the rest OK"
    )


# ============================================================================
# [F] what the sweepers REFUSE to touch -- the adversarial-review round
#
# A sweep is a DB write and cannot stop a thread, so every false positive here is
# not a cosmetic mislabel: it is two runs racing writes over one segment set, or a
# batch left un-finalized, or a live CLI run failed out from under itself.
# ============================================================================
def test_f1_a_live_thread_is_never_swept():
    """F1a. The staleness floor must not fire on a run THIS PROCESS is executing.
    Proven at the thread-name seam the guard actually reads, with a real live thread
    (a mocked predicate would assert the implementation, not the protection)."""
    import threading

    pid = _key_project(targets="de-DE")
    _add_key(pid, "live::one")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    # A row that is stale by every measure: 9h old, no StepRun activity at all.
    victim = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.RUNNING, age_hours=9)
    with Session(engine) as s:
        if not orchestrator._is_stale_run(s, s.get(WorkflowRun, victim)):
            _fail("fixture wrong: the planted row must read stale on age alone")

    stop = threading.Event()
    worker = threading.Thread(
        target=stop.wait, name=orchestrator._run_thread_name(victim, wf_id, doc_id), daemon=True
    )
    worker.start()
    try:
        if not orchestrator._run_thread_alive(victim):
            _fail("_run_thread_alive cannot see a live execution thread")
        with Session(engine) as s:
            rid = orchestrator.start_run(s, wf_id, doc_id, "de-DE")
        if _run_row(victim).status != RunStatus.RUNNING:
            _fail(
                "the staleness floor swept a run whose thread is ALIVE -- two runs "
                "now race writes over one segment set"
            )
        if rid != victim:
            _fail(f"with the corpse protected, the launch must re-attach to it, got {rid!r}")
        # ...and the startup healer refuses it too, on the same evidence.
        orchestrator.recover_interrupted_runs()
        if _run_row(victim).status != RunStatus.RUNNING:
            _fail("the startup healer swept a live-threaded run")
    finally:
        stop.set()
        worker.join(timeout=5)
    _retire(victim)
    print(
        "  [F1a] neither sweeper touches a run whose execution thread is alive in "
        "this process, however old the row OK"
    )


def test_f1_superseded_run_cannot_resurrect_itself():
    """F1b, the reviewer's probe_stale P3 shape. A sweep from ANOTHER session cannot
    stop the executing thread, so the thread has to check at the finish line. Before
    the fix it flipped its own FAILED row back to COMPLETED and flushed its segment
    write-back over the replacement run's."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "zombie::one", base="Save your changes.")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    with Session(engine) as s:
        gen = orchestrator.run_workflow_stream(s, wf_id, doc_id, "de-DE")
        first = next(gen)
        rid = first["run_id"]
        # A rival sweeper fails the row mid-run, from its own session.
        with Session(engine) as s2:
            victim = s2.get(WorkflowRun, rid)
            victim.status = RunStatus.FAILED
            victim.finished_at = datetime.utcnow()
            orchestrator._stamp_note(victim, orchestrator.RESTART_NOTE)
            s2.add(victim)
            s2.commit()
        events = [ev["type"] for ev in gen]

    row = _run_row(rid)
    if row.status != RunStatus.FAILED:
        _fail(
            f"the zombie thread resurrected its own swept row as {row.status!r} -- the sweep is "
            "advisory only"
        )
    if "done" in events:
        _fail(f"a superseded run must not emit a terminal success event, got {events!r}")
    if "error" not in events:
        _fail(f"a superseded run must say so on its stream, got {events!r}")
    if orchestrator.SUPERSEDED_ABORT_NOTE not in (row.notes or ""):
        _fail(f"the aborted run must record WHY it wrote nothing, notes={row.notes!r}")
    written = [t for _i, t in _targets(doc_id, "de-DE") if t]
    if written:
        _fail(
            "the superseded run wrote %d target(s) -- it would be racing the "  # noqa: UP031
            "replacement run over the same segments: %r" % (len(written), written)
        )
    print(
        "  [F1b] a run swept mid-flight discards its write-back instead of "
        "resurrecting itself: row stays FAILED, zero segments written OK"
    )


def test_f2_a_resumed_run_is_not_instantly_stale():
    """F2, the reviewer's probe_stale P1. A run paused overnight for human review
    kept its ORIGINAL started_at, so the instant a reviewer resumed it, it was older
    than the floor and the next launch swept a run that was at that moment
    executing."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "resume::one")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    rid = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.PAUSED, age_hours=14)
    # The pause itself is 14h old too -- the activity signal must come from the
    # RESUME, not from a StepRun the human sat on all night.
    _plant_historical_steprun(rid, wf_id, minutes_ago=14 * 60, status=RunStatus.PAUSED)

    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        run.status = RunStatus.RUNNING
        orchestrator._mark_execution_start(run)  # what every resume seam does
        s.add(run)
        s.commit()
        if orchestrator._is_stale_run(s, s.get(WorkflowRun, rid)):
            _fail(
                "a run that resumed one moment ago reads as 6h stale -- a rival "
                "launch would sweep a live, executing run"
            )
    with Session(engine) as s:
        got = orchestrator.start_run(s, wf_id, doc_id, "de-DE")
    if got != rid:
        _fail(f"the resumed run was swept and replaced by run {got!r}")
    if _run_row(rid).status != RunStatus.RUNNING:
        _fail("the resumed run was failed by the staleness floor")
    _retire(rid)
    print("  [F2] a run resumed after a 14h human pause is NOT stale and is not swept OK")


def test_n1_a_real_run_stamps_its_steprun_timestamps():
    """NEW-1(a). THE ACTIVITY SIGNAL HAS TO BE WRITTEN BY PRODUCTION, and it was not:
    `_last_activity_at` reads StepRun.started_at / finished_at, and not one of the ~19
    StepRun constructors in orchestrator.py set either column. Every row production
    wrote had both NULL, so the staleness floor and the boot healer's grace window
    fell back to pure row age -- exactly what they were changed to stop doing -- while
    this suite stayed green on hand-stamped fixtures.

    Drives a REAL three-step run and reads what the orchestrator actually persisted."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "stamp::one", base="Save your changes.")
    wf_id = _workflow("de-DE", steps=3)
    before = datetime.utcnow()
    rid = _real_run(pid, wf_id)
    after = datetime.utcnow()

    rows = _step_times(rid)
    if len(rows) != 3:
        _fail("expected 3 StepRun rows from a 3-step run, got %d" % len(rows))  # noqa: UP031
    for i, (status, started, finished) in enumerate(rows):  # noqa: B007
        if started is None or finished is None:
            _fail(
                "StepRun %d of a REAL run has started_at=%r finished_at=%r -- "  # noqa: UP031
                "nothing in production stamps them, so every activity-based guard "
                "is a no-op on real data" % (i, started, finished)
            )
        if finished < started:
            _fail("StepRun %d finished before it started (%r < %r)" % (i, finished, started))  # noqa: UP031
        if not (before <= started <= after and before <= finished <= after):
            _fail(
                "StepRun %d timestamps are outside the run's own wall clock: "  # noqa: UP031
                "%r / %r not within %r..%r" % (i, started, finished, before, after)
            )
    print(
        "  [N1a] a real 3-step run persists non-NULL, ordered, in-window "
        "started_at/finished_at on every StepRun OK"
    )


def test_n1_activity_beats_row_age_with_real_step_commits():
    """NEW-1(b), and the honest version of the old activity cell. A genuinely long
    run -- or one resumed after a pause -- has an ancient row and RECENT WORK, and
    must be judged on the work. The step commits here are production's own, not a
    fixture's invention, so this cell goes red the moment orchestrator.py stops
    stamping them."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "activity::one", base="Save your changes.")
    wf_id = _workflow("de-DE", steps=2)
    rid = _real_run(pid, wf_id)
    if any(t is None for _s, t, _f in _step_times(rid)):
        _fail("fixture wrong: the real run left unstamped StepRuns")

    # The row is 9 hours old; its newest REAL step commit is seconds old.
    _age_run_row(rid, hours=9)
    with Session(engine) as s:
        if orchestrator._is_stale_run(s, s.get(WorkflowRun, rid)):
            _fail(
                "a 9h-old row whose latest REAL step commit is seconds old reads "
                "stale -- staleness is still being measured on row age"
            )
    # ...and the control: with those real StepRuns removed, nothing says it is alive.
    with Session(engine) as s:
        for sr in s.exec(select(StepRun).where(StepRun.workflow_run_id == rid)).all():
            s.delete(sr)
        s.commit()
        if not orchestrator._is_stale_run(s, s.get(WorkflowRun, rid)):
            _fail(
                "with no activity at all the 9h row must read stale; this cell "
                "cannot tell the two apart"
            )
    _retire(rid)
    print(
        "  [N1b] a 9h-old RUNNING row whose newest PRODUCTION-written step commit "
        "is seconds old is not stale, and the same row without those commits is OK"
    )


def test_f3_staleness_floor_never_sweeps_a_batch_member():
    """F3, the reviewer's probe_stale P2. The startup healer has always excluded
    batch members; the floor did not, so it failed one behind the dispatcher's back
    and left the batch un-finalized with a wrong rollup."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "batch::one")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    with Session(engine) as s:
        batch = WorkflowBatch(
            workflow_id=wf_id, document_id=doc_id, requested_locales=["de-DE"], status="running"
        )
        s.add(batch)
        s.commit()
        s.refresh(batch)
        batch_id = batch.id
    member = _plant_run(
        doc_id, wf_id, "de-DE", status=RunStatus.RUNNING, age_hours=7, batch_id=batch_id
    )
    with Session(engine) as s:
        got = orchestrator.start_run(s, wf_id, doc_id, "de-DE")
    row = _run_row(member)
    if row.status != RunStatus.RUNNING:
        _fail(
            f"the staleness floor failed a BATCH MEMBER (status {row.status!r}) -- the batch "
            "dispatcher owns that row and its batch is now un-finalizable"
        )
    if got != member:
        _fail(f"the launch must re-attach to the live batch member, got run {got!r}")
    _retire(member)
    print(
        "  [F3] the staleness floor leaves batch members alone, exactly as the "
        "startup healer always has OK"
    )


def test_f4_startup_healer_respects_the_activity_window():
    """F4. The healer's founding assumption -- nothing is executing at startup -- is
    only true of THIS process. A synchronous CLI `workflow run` against the same DB
    keeps going while the server reboots, and stamps a StepRun per step; a true boot
    corpse's newest StepRun is as old as the crash."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "grace::one")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    live = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.RUNNING, age_hours=1)
    corpse = _plant_run(doc_id, wf_id, "fr-FR", status=RunStatus.RUNNING, age_hours=1)
    _plant_historical_steprun(live, wf_id, minutes_ago=2)  # 2nd process, mid-run
    _plant_historical_steprun(corpse, wf_id, minutes_ago=30)  # died 30 min ago

    healed = orchestrator.recover_interrupted_runs()
    if healed != 1:
        _fail(f"expected exactly the corpse healed, got {healed!r}")
    if _run_row(live).status != RunStatus.RUNNING:
        _fail(
            "the healer failed a run that wrote a step two minutes ago -- a live "
            "CLI run just lost its work to a server reboot"
        )
    if _run_row(corpse).status != RunStatus.FAILED:
        _fail("the healer left a genuine corpse (30-minute-old step) RUNNING")
    _retire(live, corpse)
    print(
        f"  [F4] the startup healer skips runs active within {orchestrator.RECOVERY_ACTIVITY_GRACE} and still fails a "  # noqa: E501
        "run whose newest step is 30 minutes old OK"
    )


def test_n1_startup_healer_spares_a_run_with_real_step_commits():
    """NEW-1(c). The same claim as [F4], but the activity it spares is written by
    PRODUCTION rather than by this file. That distinction is the finding: with the
    columns unstamped, the healer's "grace window" degraded to plain row age, and a
    second process's genuinely-live run was failed out from under itself at every
    server boot no matter how recently it had committed a step."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "grace::real", base="Save your changes.")
    wf_id = _workflow("de-DE", steps=2)
    rid = _real_run(pid, wf_id)
    stamps = _step_times(rid)
    if any(f is None for _s, _st, f in stamps):
        _fail("fixture wrong: the real run left unstamped StepRuns")
    # An hour-old row whose real work happened moments ago -- the second-process shape.
    _age_run_row(rid, hours=1)

    orchestrator.recover_interrupted_runs()
    if _run_row(rid).status != RunStatus.RUNNING:
        _fail(
            "the boot healer failed a run whose PRODUCTION-written step commit is "
            f"seconds old (now {_run_row(rid).status!r}) -- a live CLI run just lost its work to a server "  # noqa: E501
            "reboot"
        )
    if _run_row(rid).notes:
        _fail(f"the spared run was annotated: {_run_row(rid).notes!r}")
    _retire(rid)
    print(
        "  [N1c] the startup healer spares a run whose newest PRODUCTION-written "
        "step commit is inside the grace window OK"
    )


def test_f7_a_paused_run_is_never_swept_however_old():
    """F7. PAUSED is durable human state: a reviewer may come back to it days later.
    Kills the mutation that drops the status check from the floor."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "paused::old")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    ancient = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.PAUSED, age_hours=24 * 30)
    with Session(engine) as s:
        if orchestrator._is_stale_run(s, s.get(WorkflowRun, ancient)):
            _fail(
                "a PAUSED run read as stale -- the staleness floor is not scoped "
                "to RUNNING and a month-old human review is about to be destroyed"
            )
    with Session(engine) as s:
        got = orchestrator.start_run(s, wf_id, doc_id, "de-DE", detail={})
    row = _run_row(ancient)
    if row.status != RunStatus.PAUSED or row.notes:
        _fail(f"a 30-day-old PAUSED run was swept: {row.status!r} / {row.notes!r}")
    if got != ancient:
        _fail(f"the launch must re-attach to the paused run, got {got!r}")
    orchestrator.recover_interrupted_runs()
    if _run_row(ancient).status != RunStatus.PAUSED:
        _fail("the startup healer swept a PAUSED run")
    print(
        "  [F7] a PAUSED run 30 days old is swept by neither the staleness floor "
        "nor the startup healer OK"
    )


def test_f8_a_paused_re_attach_says_paused_not_running():
    """F8c. "already running" sends the operator away to wait for something that will
    never move on its own -- a paused run is waiting on a PERSON."""
    pid = _key_project(targets="de-DE, fr-FR")
    _add_key(pid, "notice::paused")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    held = _plant_run(doc_id, wf_id, "fr-FR", status=RunStatus.PAUSED)
    client = _client()
    r = _translate(client, pid, wf_id, "all")
    notice = _notice(client, r)
    if ("fr-FR is paused for review (run #%d)." % held) not in notice:  # noqa: UP031
        _fail("a paused re-attach must be reported as PAUSED, not running")
    if "fr-FR is already running" in notice:
        _fail("a paused run was described as running")
    _await_all_terminal(pid)
    print("  [F8c] a re-attach to a PAUSED run says 'paused for review', not 'already running' OK")


def test_f9_a_hostile_run_cookie_cannot_brick_the_grid():
    """F9, the reviewer's probe_misc X1. The ids are client-supplied and the cookie
    lives 30 minutes on the grid's own path, so anything that makes the handler raise
    bricks the page for half an hour -- it dies before it can clear the cookie."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "cookie::one")
    path = "/projects/%d/keys" % pid  # noqa: UP031
    from app.web import keys as keys_web

    for label, value in (
        ("25-digit id", "1" * 25),
        ("negative", "-5"),
        ("not a number", "abc"),
        ("empty tokens", ",,,"),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set(keys_web._RUNS_COOKIE, "%d|%s" % (pid, value), path=path)  # noqa: UP031
        r = client.get(path)
        if r.status_code != 200:
            _fail(
                f"{label} cookie -> HTTP {r.status_code}; the grid is bricked for the cookie's whole lifetime"  # noqa: E501
            )
        if "data-keysrun" in r.text:
            _fail(f"{label} cookie rendered a strip")
        if "loc_keys_runs=" not in (r.headers.get("set-cookie") or ""):
            _fail(
                f"{label} cookie was not CLEARED, so it is re-sent on every request until "
                "it expires"
            )
    print(
        "  [F9] a hostile run-id cookie (25-digit / negative / non-numeric / empty) "
        "answers 200, renders no strip, and is cleared OK"
    )


# ============================================================================
# [C] the grid hand-off (#47)
# ============================================================================
def test_c_translate_hands_run_ids_to_the_grid_exactly_once():
    pid = _key_project(targets="de-DE, fr-FR")
    _add_key(pid, "strip::one")
    wf_id = _workflow("de-DE")
    client = _client()
    r = _translate(client, pid, wf_id, "all")
    if r.status_code != 303:
        _fail(f"translate-all answered {r.status_code}")
    cookie = r.headers.get("set-cookie", "")
    if "loc_keys_runs=" not in cookie:
        _fail(
            "the launched run ids must ride to the grid for the progress strip, "
            f"got Set-Cookie {cookie[:200]!r}"
        )
    launched = sorted(x.id for x in _runs_for(pid, "de-DE") + _runs_for(pid, "fr-FR"))
    for rid in launched:
        if str(rid) not in cookie:
            _fail("run %d was launched but is not in the hand-off: %r" % (rid, cookie))  # noqa: UP031

    html = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    if "data-keysrun" not in html:
        _fail("the grid did not render the progress strip container")
    if "/static/keysrun.js" not in html:
        _fail("the grid did not load the progress widget")
    for rid in launched:
        if ('data-run-id="%d"' % rid) not in html:  # noqa: UP031
            _fail("run %d has no row in the progress strip: strip missing ids" % rid)  # noqa: UP031
    for attr in (
        "data-run-state",
        "data-run-steps",
        "data-run-error",
        'data-stream-url="/runs/stream"',
    ):
        if attr not in html:
            _fail(f"the strip is missing the {attr!r} hook the widget reads")

    # ...and EXACTLY ONCE: the widget reloads the grid when every run is terminal, so
    # ids that survived the render would make that reload loop forever.
    again = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    if "data-keysrun" in again:
        _fail(
            "the run-id hand-off was not consumed -- the post-completion auto-reload "
            "would re-render the strip and reload again, forever"
        )
    _await_all_terminal(pid)
    print(
        "  [C] the translate POST hands the launched run ids to the grid, the grid "
        "renders one strip row per run with the widget's data-* hooks, and the "
        "hand-off is consumed exactly once OK"
    )


def test_c_truncated_tracking_is_declared():
    """NEW-4. The strip tracks at most _RUNS_COOKIE_MAX runs because each row holds a
    browser connection open. Past that the runs are unaffected but they are NOT being
    watched -- and a strip that silently showed six of nine would be a smaller version
    of the very "you were not told" defect this change exists to fix."""
    from app.web import keys as keys_web

    cap = keys_web._RUNS_COOKIE_MAX
    locales = ["de-DE", "fr-FR", "es-419", "it-IT", "pt-BR", "nl-NL", "pl-PL", "sv-SE"][: cap + 2]
    pid = _key_project(targets=", ".join(locales))
    _add_key(pid, "trunc::one")
    wf_id = _workflow("de-DE")
    client = _client()
    r = _translate(client, pid, wf_id, "all")
    if r.status_code != 303:
        _fail(f"translate-all answered {r.status_code}")
    # ONE GET: the run-id hand-off is a one-shot cookie, so a second request would
    # find the strip already consumed and read zero rows for the wrong reason.
    html = client.get(r.headers["location"]).text
    notice = (
        html.split('<p class="ok">', 1)[1].split("</p>", 1)[0].strip()
        if '<p class="ok">' in html
        else ""
    )
    # Queued rows are static pills, so only admitted runs count against the SSE cap.
    import re

    admitted = int(re.search(r"Started (\d+) run", notice).group(1))
    expect = "(+%d more not tracked below.)" % (admitted - cap)  # noqa: UP031
    if expect not in notice:
        _fail(
            f"a fan-out wider than the strip's cap must say so (expected {expect!r}), got {notice!r}"  # noqa: E501
        )
    rows = html.count('data-run-id="')
    if rows != cap:
        _fail("the strip rendered %d rows, expected the cap of %d" % (rows, cap))  # noqa: UP031
    _await_all_terminal(pid)
    print(
        "  [C] a fan-out wider than the strip's %d-row cap says '(+%d more not "  # noqa: UP031
        "tracked below.)' rather than quietly showing a subset OK" % (cap, len(locales) - cap)
    )


def test_c_forged_run_ids_render_nothing():
    """The ids arrive in a client-settable cookie. A run that is not this project's
    must not put its locale on this project's page."""
    victim = _key_project(targets="de-DE")
    _add_key(victim, "forge::victim")
    other = _key_project(targets="fr-FR")
    _add_key(other, "forge::other")
    foreign = _plant_run(_ns_doc(other), _workflow("fr-FR"), "fr-FR")

    client = TestClient(app)
    client.cookies.set(
        "loc_keys_runs",
        "%d|%d,999999" % (victim, foreign),  # noqa: UP031
        path="/projects/%d/keys" % victim,  # noqa: UP031
    )
    html = client.get("/projects/%d/keys" % victim).text  # noqa: UP031
    if "data-keysrun" in html:
        _fail("a forged cookie naming another project's run rendered a strip")
    print(
        "  [C] a forged run-id cookie (a foreign run + a nonexistent id) renders no strip rows OK"
    )


# ============================================================================
# [D] the failure is on the wire
# ============================================================================
def _closed_event(run_id):
    for _run, ev in orchestrator.observe_run(run_id, poll_interval=0.01, max_seconds=5):
        if ev.get("type") == "closed":
            return ev
    return None


def test_d_failed_run_puts_its_step_error_on_the_stream():
    pid = _key_project(targets="de-DE")
    _add_key(pid, "wire::one")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    rid = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.FAILED, finished=True)
    boom = (
        "step 'fix' (en-US->de-DE): APIError - Context size has been exceeded. "
        "<img src=x onerror=alert(1)>"
    )
    with Session(engine) as s:
        step = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).first()
        s.add(StepRun(workflow_run_id=rid, step_id=step.id, status=RunStatus.FAILED, error=boom))
        s.commit()

    ev = _closed_event(rid)
    if ev is None:
        _fail("the stream never closed")
    if ev.get("status") != RunStatus.FAILED:
        _fail(f"the closed event must carry the run's verdict, got {ev!r}")
    if boom not in (ev.get("error") or ""):
        _fail(
            "the closed event must carry the step error text (this IS the "
            "silent-failure fix), got {!r}".format(ev.get("error"))
        )
    print(
        "  [D] a FAILED run's `closed` event carries status=failed AND the step "
        "error text the widget renders OK"
    )


def test_d_restart_swept_run_explains_itself_with_no_steprun():
    """A run failed OUTSIDE the step machinery has no StepRun to blame -- exactly the
    restart corpse. Its own note is the fallback, or the strip says nothing again."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "wire::two")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    rid = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.RUNNING, age_hours=1)
    orchestrator.recover_interrupted_runs()
    ev = _closed_event(rid)
    if ev is None or ev.get("status") != RunStatus.FAILED:
        _fail(f"a swept run must stream a FAILED verdict, got {ev!r}")
    if orchestrator.RESTART_NOTE not in (ev.get("error") or ""):
        _fail(f"a run with no StepRun must fall back to its own note, got {ev!r}")
    print(
        "  [D] a restart-swept run (no StepRun at all) still explains itself on the "
        "stream, from its note OK"
    )


def test_d_live_pause_close_carries_the_verdict():
    """NEW-2. The live-pause branch was the LAST `closed` site still emitting a bare
    event -- and it is the NORMAL path for a freshly-connected paused run, i.e. the
    most common paused case there is. A consumer reading the status saw none, treated
    the close as an expired observer window, reconnected, burned its second window
    and relabelled a PAUSED run "still running"."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "wire::paused")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    rid = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.PAUSED)
    with Session(engine) as s:
        step = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).first()
        run = s.get(WorkflowRun, rid)
        run.current_step_index = step.order_index
        s.add(run)
        s.add(
            StepRun(
                workflow_run_id=rid,
                step_id=step.id,
                status=RunStatus.PAUSED,
                input_text="please review",
            )
        )
        s.commit()

    events = [ev for _r, ev in orchestrator.observe_run(rid, poll_interval=0.01, max_seconds=5)]
    kinds = [e.get("type") for e in events]
    if "paused" not in kinds:
        _fail(f"the paused run did not stream a 'paused' event: {kinds!r}")
    closed = [e for e in events if e.get("type") == "closed"]
    if not closed:
        _fail(f"the stream never closed: {kinds!r}")
    ev = closed[-1]
    if ev.get("status") != RunStatus.PAUSED:
        _fail(f"the live-pause close must carry status=paused, got {ev!r}")
    if ev.get("run_id") != rid:
        _fail(f"the live-pause close must name its run, got {ev!r}")
    print(
        "  [D] the LIVE-pause close carries run_id + status=paused, so a consumer "
        "cannot mistake it for an expired observer window OK"
    )


def test_d_completed_run_closes_clean():
    """The other half: a COMPLETED run must NOT look like a failure, and the closed
    event must stay additive (run_view.html reads none of the new fields)."""
    pid = _key_project(targets="de-DE")
    _add_key(pid, "wire::three", base="Save your changes.")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wf_id, doc_id, "de-DE")
    if _await_terminal(rid) != RunStatus.COMPLETED:
        _fail("the fixture run did not complete")
    ev = _closed_event(rid)
    if ev.get("status") != RunStatus.COMPLETED or ev.get("error"):
        _fail(f"a completed run must close clean, got {ev!r}")
    print("  [D] a COMPLETED run closes with status=completed and no error OK")


# ============================================================================
# [G] the widget's own decisions, and the run view's escaping
# ============================================================================
def test_g_widget_behaviour_probe():
    """The strip's rules are DECISIONS, not styling: when it may reload the grid out
    from under the operator, whether a failure or a "waiting on a human" survives on
    screen, what it does when the server's observer window expires, how it counts
    replayed steps, and how many connections it holds. None of that is visible in
    markup, so tests/keysrun_probe.js runs the REAL app/static/keysrun.js in a
    minimal DOM and drives the exact event sequences observe_run emits.

    Skipped with a loud line when `node` is not installed rather than failing the
    suite -- the Python surfaces are asserted above either way."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        print(
            "  [G] widget probe SKIPPED (node is not on PATH) -- run "
            "`node tests/keysrun_probe.js` on a box that has it"
        )
        return
    probe = os.path.join(_ROOT, "tests", "keysrun_probe.js")
    proc = subprocess.run([node, probe], cwd=_ROOT, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(proc.stdout[-4000:])
        print(proc.stderr[-2000:])
        _fail("the keysrun.js behaviour probe failed")
    checks = proc.stdout.count("  ok: ")
    if checks < 20:
        _fail("the widget probe only ran %d checks -- it is not exercising the widget" % checks)  # noqa: UP031
    print(
        "  [G] app/static/keysrun.js passes %d behaviour checks: all-clean reloads "  # noqa: UP031
        "once, a FAILED or PAUSED run blocks the reload, an expired observer "
        "window reconnects once then hands over, replayed steps do not inflate the "
        "counter, and at most 5 streams are open at a time OK" % checks
    )


def test_g_run_view_escapes_provider_error_text():
    """Pre-existing STORED XSS (review F13), fixed while in this surface.
    run_view.html spliced ev.error / ev.step_name / ev.provider / ev.model straight
    into innerHTML while carefully escaping only ev.output. A provider error is
    attacker-influenceable text, it is PERSISTED on the StepRun row, and the page
    replays it from that row on every visit -- so a script-bearing error string was
    stored script on an authenticated page.

    Asserted on the shipped template, because the vulnerable expression is the
    template's own JavaScript: there is no server-rendered HTML to inspect."""
    import re as _re

    src = open(os.path.join(_ROOT, "app", "templates", "run_view.html"), encoding="utf-8").read()  # noqa: SIM115
    with open(os.path.join(_ROOT, "app", "templates", "_html_escape.js"), encoding="utf-8") as f:
        src = src.replace("{% include '_html_escape.js' %}", f.read())
    if "function esc(" not in src:
        _fail("run_view.html has no escaper at all")
    # EVERY interpolation in the step block, not a hand-listed few: a future field
    # spliced in unescaped must fail this too, which is the whole point.
    block = _re.search(r"div\.innerHTML = (.*?);\n", src, _re.S)
    if not block:
        _fail("could not find the step-row innerHTML assignment")
    holes = _re.findall(r"\$\{([^}]*)\}", block.group(1))
    if len(holes) < 6:
        _fail(f"expected the step row to interpolate several fields, found {holes!r}")
    for hole in holes:
        if "esc(" not in hole:
            _fail(
                f"run_view.html splices {hole!r} into innerHTML UNESCAPED -- a "
                "provider-supplied string is stored script on this page"
            )
    if not any("ev.error" in h for h in holes):
        _fail("the step row no longer renders ev.error; this cell is now vacuous")
    # ...and the escaper really neutralises the payload class in question.
    body = _re.search(r"const ESCAPES = \{(.*?)\};", src, _re.S)
    if not body:
        _fail("could not find the escape table")
    for ch in ("'<'", "'>'", "'&'", "'\"'", '"\'"'):
        if ch not in body.group(1):
            _fail(
                f"the escape table is missing {ch} -- an attribute-context "
                "interpolation would reopen this"
            )
    print(
        "  [G] run_view.html escapes every server-supplied field it splices into "
        "innerHTML (error / step_name / step_type / provider / model / output), "
        "not just output OK"
    )


# ============================================================================
# [E] stream auth (login wall ON -- with it off this cell is decoration)
# ============================================================================
def _login(email, role=Role.ADMIN, customer_id=None):
    security.create_user(email, email, role, PW, customer_id=customer_id)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    if r.status_code != 303:
        _fail(f"login failed for {email}: {r.status_code}")
    csrf_for(c)
    return c


def test_e_stream_auth_matches_the_grid_ring():
    pid = _key_project(targets="de-DE")
    _add_key(pid, "auth::one")
    doc_id = _ns_doc(pid)
    wf_id = _workflow("de-DE")
    # A terminal run so the observer replays and closes immediately (an in-flight run
    # would hold the connection for the whole poll window).
    rid = _plant_run(doc_id, wf_id, "de-DE", status=RunStatus.FAILED, finished=True)

    _auth(True)
    try:
        member = _login("runrecov-member@x.com", role=Role.VIEWER)
        with Session(engine) as s:
            user = security.get_by_email("runrecov-member@x.com")
            s.add(ProjectMember(project_id=pid, user_id=user.id))
            s.commit()
        r = member.get("/runs/stream?run_id=%d" % rid)  # noqa: UP031
        if r.status_code != 200:
            _fail(
                "a project member who can view the grid must be able to stream its "
                f"runs, got {r.status_code}"
            )
        if "closed" not in r.text:
            _fail(f"the member's stream carried no events: {r.text[:200]!r}")

        outsider = _login("runrecov-outsider@x.com", role=Role.VIEWER)
        r = outsider.get("/runs/stream?run_id=%d" % rid, follow_redirects=False)  # noqa: UP031
        if r.status_code == 200:
            _fail(f"a user outside the project's ring streamed its run ({r.text[:200]})")
        if r.status_code not in (401, 403, 404, 303, 307):
            _fail(f"unexpected refusal code for an outsider: {r.status_code}")

        anon = TestClient(app)
        r = anon.get("/runs/stream?run_id=%d" % rid, follow_redirects=False)  # noqa: UP031
        if r.status_code == 200:
            _fail("an anonymous caller streamed a run")
    finally:
        _auth(False)
    print(
        "  [E] a project member may stream a grid-launched run; an out-of-ring "
        "user and an anonymous caller may not OK"
    )


def main():
    init_db()
    _switch(True)
    test_a_recovery_fails_the_orphan_and_unblocks_the_locale()
    test_a_paused_and_batch_members_are_left_alone()
    test_b_fresh_running_run_is_reported_not_counted_as_started()
    test_b_stale_running_run_is_swept_not_reattached()
    test_b_detail_out_parameter_distinguishes_the_two()
    test_213_a_different_workflow_is_refused_while_a_run_is_in_flight()
    test_213_same_workflow_still_reattaches_and_a_terminal_rival_releases()
    test_213_a_stale_rival_corpse_does_not_hold_the_door()
    test_213_queue_runs_reports_the_conflicting_pair_and_runs_the_rest()
    test_f1_a_live_thread_is_never_swept()
    test_f1_superseded_run_cannot_resurrect_itself()
    test_f2_a_resumed_run_is_not_instantly_stale()
    test_n1_a_real_run_stamps_its_steprun_timestamps()
    test_n1_activity_beats_row_age_with_real_step_commits()
    test_f3_staleness_floor_never_sweeps_a_batch_member()
    test_f4_startup_healer_respects_the_activity_window()
    test_n1_startup_healer_spares_a_run_with_real_step_commits()
    test_f7_a_paused_run_is_never_swept_however_old()
    test_f8_a_paused_re_attach_says_paused_not_running()
    test_f9_a_hostile_run_cookie_cannot_brick_the_grid()
    test_c_translate_hands_run_ids_to_the_grid_exactly_once()
    test_c_truncated_tracking_is_declared()
    test_c_forged_run_ids_render_nothing()
    test_d_failed_run_puts_its_step_error_on_the_stream()
    test_d_restart_swept_run_explains_itself_with_no_steprun()
    test_d_live_pause_close_carries_the_verdict()
    test_d_completed_run_closes_clean()
    test_g_widget_behaviour_probe()
    test_g_run_view_escapes_provider_error_text()
    test_e_stream_auth_matches_the_grid_ring()
    print("RUN RECOVERY PROGRESS OK")


if __name__ == "__main__":
    main()
