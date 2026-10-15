"""QA-1 regression: a graph/correction-loop run must NOT die with SQLite
'database is locked' on a non-exempt (priced) provider (offline, hermetic).

Root cause (verified report QA-WORKFLOWS-2026-07-19): the RunGuard writes
WorkflowRun.context via `session.add(run)` WITHOUT committing; the loop's next ORM
read autoflushes that UPDATE, taking SQLite's single writer lock UNCOMMITTED and
holding it straight through the step's provider call. Inside that call
spend.record_usage opens its OWN Session (same thread, sequential) to write the
ledger and blocks on that very lock -- a true circular wait the orchestrator can
only escape by committing first. A PRICED provider is what makes record_usage open
the contended write at all (a mock/local one is exempt -> priced 0 -> no write); the
autoflush trigger is `tm.lookup_batch` for the order-0 translate step (proved to kill
step 1 live) and `_eval_run_condition` for a conditional fix step.

The CURE is layer (a): RunGuard.enter() commits the scratchpad BEFORE the provider
call, releasing the writer lock. (Layer (b), the explicit PRAGMA busy_timeout in
db.py, is belt-and-suspenders only: a busy_timeout can DELAY but never resolve a
same-thread circular wait -- the report's "locked after 5.16s" is that ~5s DBAPI-
default timeout expiring -- so it is asserted for presence, not as the fix.)

  (B) test_graph_workflow_priced_provider_completes -- THE regression: a graph
      workflow whose order-0 translate step runs on a PRICED provider completes and
      accrues spend instead of deadlocking. FAILS against the pre-fix orchestrator
      (reconstruct via `git show HEAD:app/services/orchestrator.py`); passes after.
  (C) test_busy_timeout_floor_installed -- the explicit writer-wait floor
      (db.BUSY_TIMEOUT_MS) is installed on the connection (defense-in-depth guard).

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys, ASCII-only prints. A
PRICED fake provider (NOT the mock, NOT local -- both are is_exempt and price 0 after
QA-4, which would HIDE the bug) drives the ledger write the report's warning names.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_spendlock_")

from sqlmodel import Session, select

from app import db as _db
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    Segment,
    SegStatus,
    SpendLedger,
    StepType,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import orchestrator, settings
from app.services import providers as _prov
from app.services import spend as spend_svc
from app.services.providers import Provider, register_provider


class PricedStub(Provider):
    """A CONFIGURED (non-mock, non-local) engine -> spend.is_exempt is False, so a
    completed call accrues real ledger spend (opus-tier default pricing). This is the
    'fake priced provider, not mock' the report's warning demands: with QA-4 fixed a
    local engine prices 0, so the deadlock must be proved with a genuinely-priced one."""

    name = "claude"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        PricedStub.calls += 1
        self.last_usage = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "[es] " + (payload or {}).get("text", "hola")


def _reset_ledger():
    key = spend_svc.period_key()
    with Session(engine) as s:
        row = s.get(SpendLedger, key)
        if row:
            row.accumulated_usd = 0.0
            s.add(row)
            s.commit()
    spend_svc._tripped_audited.clear()


def test_graph_workflow_priced_provider_completes():
    """(B) A GRAPH workflow (guard active) whose ORDER-0 translate step runs on a
    PRICED provider completes and accrues spend, instead of deadlocking. Pre-fix the
    guard's uncommitted run.context write was autoflushed by tm.lookup_batch and held
    across the provider call, so spend.record_usage blocked on the writer lock and the
    run ended FAILED 'database is locked' (the verified live repro: step 1 = translate).
    Post-fix RunGuard.enter() commits the scratchpad first, releasing the lock."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})  # cap OFF -> no spend-gate interference
    register_provider("claude", PricedStub())
    PricedStub.calls = 0
    try:
        with Session(engine) as s:
            p = Project(name="spendlock", source_locale="en-US")
            s.add(p)
            s.commit()
            s.refresh(p)
            pid = p.id

            doc = Document(project_id=pid, filename="d.txt", mode=TransMode.SEGMENTED)
            s.add(doc)
            s.commit()
            s.refresh(doc)
            doc_id = doc.id
            s.add(
                Segment(
                    document_id=doc_id,
                    target_locale="es-ES",
                    order_index=0,
                    source_text="Hello world.",
                    status=SegStatus.NEW,
                )
            )
            s.commit()

            wf = Workflow(name="graph priced translate", scope="global")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            wf_id = wf.id
            # ONE llm_agent translate step at order 0 on the PRICED engine, with
            # max_visits set so _has_graph() is True -> the RunGuard is ACTIVE and
            # writes run.context (the deadlock precondition). No branch -> runs once.
            s.add(
                WorkflowStep(
                    workflow_id=wf_id,
                    order_index=0,
                    name="Translate",
                    provider="claude",
                    step_type=StepType.LLM_AGENT,
                    prompt_text="Translate: {text}",
                    chunk_policy="none",
                    retry_max=1,
                    backoff_base=0.0,
                    max_visits=2,
                )
            )
            s.commit()

        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")

        assert "database is locked" not in (summary.get("error") or ""), (
            "QA-1 regression: the run deadlocked on the priced provider",
            summary,
        )
        assert summary["status"] == "completed", (
            "a graph workflow on a priced provider must COMPLETE",
            summary,
        )
        assert PricedStub.calls > 0, ("the priced provider must have been called", PricedStub.calls)
        assert spend_svc.spent_this_period() > 0, (
            "a priced call must have accrued spend (the nested ledger write succeeded)"
        )
        with Session(engine) as s:
            seg = s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "es-ES"
                )
            ).first()
            assert seg is not None and (seg.target_text or "").strip(), (
                "the completed run must have written a target",
                seg and seg.target_text,
            )
    finally:
        _prov._OVERRIDES.pop("claude", None)
        settings.set_many({"spend_cap_usd": "0"})
        _reset_ledger()
    print(
        "  (B) graph workflow on a PRICED provider completes + accrues spend "
        "(no 'database is locked' on the order-0 translate step)"
    )


def test_busy_timeout_floor_installed():
    """(C) The explicit writer-wait floor is installed on every SQLite connection
    (defense-in-depth). Pre-fix db.py set no such pragma / constant. NB pysqlite's
    connect() already applies a ~5s busy_timeout by default, so this asserts the
    EXPLICIT, owned guarantee (db.BUSY_TIMEOUT_MS) is present and non-zero, not that
    it changed behaviour -- the real deadlock cure is the orchestrator commit (B)."""
    with engine.connect() as conn:
        val = int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar())
    assert _db.BUSY_TIMEOUT_MS >= 2000, ("floor must be a few seconds", _db.BUSY_TIMEOUT_MS)
    assert val == _db.BUSY_TIMEOUT_MS and val > 0, (
        "the explicit busy_timeout floor must be installed",
        val,
        _db.BUSY_TIMEOUT_MS,
    )
    print("  (C) explicit PRAGMA busy_timeout floor installed: %d ms" % val)  # noqa: UP031


def main():
    init_db()
    settings.set_many({"spend_cap_usd": "0"})
    test_busy_timeout_floor_installed()
    test_graph_workflow_priced_provider_completes()
    settings.set_many({"spend_cap_usd": "0"})
    print("ORCHESTRATOR SPEND LOCK OK")


if __name__ == "__main__":
    main()
