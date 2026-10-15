"""Real per-workflow spend tracking (feat/workflow-spend), offline + hermetic.

StepRun now captures the ACTUAL per-step-visit spend (cost_usd/tokens_in/tokens_out)
so `report --group-by workflow` attributes EXACT per-workflow cost from real
WorkflowRun/StepRun sums instead of the old most-recent-run job-cost approximation.

Coverage:
  (1) migration both paths: the three columns exist (fresh create_all), the SQLite
      non-alembic shim mirror (db._ADDED_COLUMNS["steprun"]) carries them, the alembic
      revision chains off the prior head, and a legacy StepRun (inserted without them)
      reads NULL.
  (2) a PRICED-provider run stamps EXACT cost/tokens per step-visit, and the stamped
      cost equals the SAME figure the spend ledger accrued (shared pricing seam).
  (3) MockProvider stamps honest zeros (0.0 cost) + real token counts, and never
      accrues to the ledger (exempt) — the load-bearing exemption is unchanged.
  (4) a PARALLEL-segment step accumulates EXACTLY across the fan-out (no lost updates
      under the segment executor — deterministic).
  (5) two distinct steps stamp INDEPENDENT costs; a correction LOOP stamps two+
      independent StepRun rows for the SAME step (one per visit).
  (6) a paused-then-resumed run stamps BOTH visits honestly.
  (7) show-run surfaces (step_states + visit_history via run_overlay) carry the fields;
      a legacy/untracked row reads NULL.

A PRICED fake provider (NOT the mock / local — both are is_exempt and price 0) is what
drives a real ledger write, mirroring verify_orchestrator_spend_lock's pattern.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfspend_")

from types import SimpleNamespace

from sqlmodel import Session, select

from app import db as _db
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    Segment,
    SegStatus,
    SpendLedger,
    StepRun,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, pricing, qe, run_overlay, settings
from app.services import providers as _prov
from app.services import spend as spend_svc
from app.services.providers import (
    Provider,
    ProviderSpec,
    register_provider,
)


# --- a genuinely-priced provider (NOT mock/local) --------------------------------
class PricedStub(Provider):
    """CONFIGURED engine -> spend.is_exempt False -> a completed call accrues real
    ledger spend. Deterministic fixed usage so cost is exactly reproducible: with the
    opus-tier default rates (5/25 per Mtoken) one call costs
    (1000*5 + 200*25)/1e6 = 0.0100 USD."""

    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "[es] " + (payload or {}).get("text", "hola")


PER_CALL_TIN, PER_CALL_TOUT = 1000, 200
# Pin the model so pricing is deterministic (opus-tier 5/25 -> one call = 0.0100 USD),
# independent of whatever config.DEFAULT_MODELS resolves for a bare "claude".
PRICED_MODEL = "claude-opus-4-8"
PER_CALL_COST = pricing.cost_usd(
    "claude", PRICED_MODEL, {"input_tokens": 1000, "output_tokens": 200}
)


class LqaAlwaysFail(Provider):
    """Forces an LQA gate to FAIL every visit (critical MQM error) so a branch loops."""

    name = "lqafail"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return '{"errors":[{"dimension":"accuracy","severity":"critical","note":"bad"}]}'


class PricedJunkJudge(Provider):
    """A PRICED judge that returns NON-JSON (a live-provider refusal shape) -> the QE
    parse soft-skips as junk_json AFTER charging the ledger. The leak: that skip must
    still carry the real cost."""

    name = "junkjudge"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "I'm sorry, I can't help with that."


class PricedWhitespaceJudge(Provider):
    """A PRICED judge whose call SUCCEEDS (charges the ledger) but returns whitespace-
    only text -> the provider_error guard soft-skips. That skip must still carry cost."""

    name = "wsjudge"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "   "


# --- tiny fixtures (direct ORM, no upload/docx) ----------------------------------
def _reset_ledger():
    with Session(engine) as s:
        row = s.get(SpendLedger, spend_svc.period_key())
        if row:
            row.accumulated_usd = 0.0
            s.add(row)
            s.commit()
    spend_svc._tripped_audited.clear()


def _mk_doc(n_segments=1, provider="claude", prefix="wfspend"):
    with Session(engine) as s:
        p = Project(name=f"{prefix}-proj", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        doc = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i in range(n_segments):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=f"Sentence {i}.",
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return doc.id


def _mk_full_text_doc(text="Hello world.", prefix="ft"):
    with Session(engine) as s:
        p = Project(name=f"{prefix}-proj", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        doc = Document(
            project_id=p.id,
            filename="d.txt",
            mode=TransMode.FULL_TEXT,
            skeleton={"units": [{"text": text}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc.id


def _add_step(wf_id, order, name, **kw):
    with Session(engine) as s:
        s.add(
            WorkflowStep(
                workflow_id=wf_id,
                order_index=order,
                name=name,
                provider=kw.pop("provider", "claude"),
                model=kw.pop("model", PRICED_MODEL),
                step_type=kw.pop("step_type", StepType.LLM_AGENT),
                prompt_text=kw.pop("prompt_text", "Translate: {text}"),
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
                **kw,
            )
        )
        s.commit()


def _new_wf(name):
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        return wf.id


def _stepruns(run_id):
    with Session(engine) as s:
        return s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()


def _approx(a, b, tol=1e-9):
    return abs((a or 0.0) - (b or 0.0)) <= tol


# ============================================================================
def test_migration_columns_and_legacy_null():
    """(1) The three columns exist on both schema paths and a legacy row reads NULL."""
    # SQLite non-alembic shim mirror carries the columns (the upgrade path db.py applies
    # on a boot whose real DB never had alembic).
    cols = dict(_db._ADDED_COLUMNS["steprun"])
    for c in ("cost_usd", "tokens_in", "tokens_out"):
        assert c in cols, f"db._ADDED_COLUMNS['steprun'] missing {c}"
    assert cols["cost_usd"].upper().startswith("FLOAT")
    assert cols["tokens_in"].upper().startswith("INTEGER")
    # The alembic revision chains off the prior head f9c2e7a4d1b8 (fresh-DB upgrade path).
    import importlib.util

    _mp = os.path.join(_ROOT, "alembic", "versions", "fa3d9c1e7b52_steprun_spend.py")
    _spec = importlib.util.spec_from_file_location("_mig_wfspend", _mp)
    m = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(m)
    assert m.revision == "fa3d9c1e7b52" and m.down_revision == "f9c2e7a4d1b8", (
        m.revision,
        m.down_revision,
    )
    # Fresh create_all path: a StepRun inserted WITHOUT the new fields reads NULL
    # (honest "pre-tracking / unknown", no backfill).
    wf_id = _new_wf("mig-legacy")
    _add_step(wf_id, 0, "T")
    with Session(engine) as s:
        st = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).first()
        run = WorkflowRun(
            workflow_id=wf_id,
            document_id=_mk_doc(1, prefix="mig"),
            target_locale="es-ES",
            status="completed",
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        legacy = StepRun(workflow_run_id=run.id, step_id=st.id, status="completed")
        s.add(legacy)
        s.commit()
        s.refresh(legacy)
        assert legacy.cost_usd is None and legacy.tokens_in is None and legacy.tokens_out is None, (
            "a legacy StepRun must read NULL, never 0"
        )
    print(
        "  (1) migration: columns on both paths (shim mirror + alembic chain), "
        "legacy StepRun reads NULL OK"
    )


def test_priced_run_stamps_exact_and_matches_ledger():
    """(2) A priced single-segment run stamps EXACT cost/tokens, and the stamped cost
    equals the ledger delta (same pricing computation — they cannot diverge)."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})
    register_provider("claude", PricedStub())
    try:
        before = spend_svc.spent_this_period()
        doc_id = _mk_doc(1, prefix="exact")
        wf_id = _new_wf("exact")
        _add_step(wf_id, 0, "Translate")
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
        assert summary["status"] == "completed", summary
        srs = _stepruns(summary["run_id"])
        assert len(srs) == 1, srs
        sr = srs[0]
        assert sr.tokens_in == PER_CALL_TIN and sr.tokens_out == PER_CALL_TOUT, (
            sr.tokens_in,
            sr.tokens_out,
        )
        assert _approx(sr.cost_usd, PER_CALL_COST), (sr.cost_usd, PER_CALL_COST)
        ledger_delta = spend_svc.spent_this_period() - before
        assert _approx(sr.cost_usd, ledger_delta), (
            "stamped StepRun cost must equal the ledger delta (shared pricing seam)",
            sr.cost_usd,
            ledger_delta,
        )
        assert sr.cost_usd > 0
    finally:
        _prov._OVERRIDES.pop("claude", None)
        _reset_ledger()
    print("  (2) priced run stamps EXACT cost/tokens == ledger delta OK")


def test_mock_run_honest_zeros_and_no_accrual():
    """(3) The mock stamps 0.0 cost (honest, not NULL) + real-ish token counts, and
    NEVER accrues to the ledger — the load-bearing exemption is untouched."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})
    before = spend_svc.spent_this_period()
    doc_id = _mk_doc(1, provider="mock", prefix="mock")
    wf_id = _new_wf("mock")
    _add_step(wf_id, 0, "Translate", provider="mock")
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    sr = _stepruns(summary["run_id"])[0]
    assert sr.cost_usd == 0.0, ("mock stamps an honest 0.0, never NULL", sr.cost_usd)
    assert sr.tokens_in is not None and sr.tokens_out is not None
    assert spend_svc.spent_this_period() == before, "the mock must NOT accrue spend"
    print("  (3) mock run: 0.0 cost stamped, real token counts, no ledger accrual OK")


def test_parallel_segments_accumulate_exactly():
    """(4) A multi-segment step fans out per-segment provider calls on the executor;
    the per-visit accumulation must be EXACT (no lost updates). 3 segments -> the step
    cost is exactly 3x the per-call cost and the ledger accrues the same."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0", "mt_concurrency": "4"})
    register_provider("claude", PricedStub())
    try:
        before = spend_svc.spent_this_period()
        n = 3
        doc_id = _mk_doc(n, prefix="parallel")
        wf_id = _new_wf("parallel")
        _add_step(wf_id, 0, "Translate")
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
        assert summary["status"] == "completed", summary
        sr = _stepruns(summary["run_id"])[0]
        assert sr.tokens_in == n * PER_CALL_TIN and sr.tokens_out == n * PER_CALL_TOUT, (
            "no lost updates across the fan-out",
            sr.tokens_in,
            sr.tokens_out,
        )
        assert _approx(sr.cost_usd, n * PER_CALL_COST), (sr.cost_usd, n * PER_CALL_COST)
        assert _approx(spend_svc.spent_this_period() - before, n * PER_CALL_COST)
    finally:
        _prov._OVERRIDES.pop("claude", None)
        settings.set_many({"mt_concurrency": "4"})
        _reset_ledger()
    print("  (4) parallel-segment step accumulates EXACTLY (3x per-call, no lost updates) OK")


def test_two_steps_independent_costs():
    """(5a) Two distinct llm_agent steps each stamp their OWN independent cost (the
    per-step accumulator is reset each step-visit, never cumulative)."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})
    register_provider("claude", PricedStub())
    try:
        doc_id = _mk_doc(1, prefix="twostep")
        wf_id = _new_wf("twostep")
        _add_step(wf_id, 0, "Translate")
        _add_step(wf_id, 1, "Review", prompt_text="Review: {text}")
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
        assert summary["status"] == "completed", summary
        srs = _stepruns(summary["run_id"])
        assert len(srs) == 2, srs
        for sr in srs:
            assert _approx(sr.cost_usd, PER_CALL_COST), (
                "each step stamps its OWN per-visit cost, not a cumulative total",
                sr.cost_usd,
            )
    finally:
        _prov._OVERRIDES.pop("claude", None)
        _reset_ledger()
    print("  (5a) two steps stamp INDEPENDENT per-step costs OK")


def test_loop_two_visits_independent_costs():
    """(5b) A correction LOOP (LQA always fails -> route back to Fix, fix.max_visits=2)
    stamps a SEPARATE StepRun for EACH visit of the SAME step, each with its own
    independent (non-cumulative) cost."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})
    register_provider("claude", PricedStub())
    register_provider("lqafail", LqaAlwaysFail())
    try:
        doc_id = _mk_doc(1, prefix="loop")
        wf_id = _new_wf("loop")
        _add_step(wf_id, 0, "Translate")
        _add_step(wf_id, 1, "Fix", key="fix", prompt_text="Fix: {text}", max_visits=2)
        _add_step(
            wf_id,
            2,
            "LQA",
            key="lqa",
            step_type=StepType.LQA,
            provider="lqafail",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
        assert summary["status"] == "completed", summary
        with Session(engine) as s:
            steps = orchestrator.get_steps(s, wf_id)
        fix_id = steps[1].id
        fix_runs = [sr for sr in _stepruns(summary["run_id"]) if sr.step_id == fix_id]
        assert len(fix_runs) == 2, (
            "the Fix step must have TWO stamped visits",
            [sr.outcome_index for sr in fix_runs],
        )
        assert [sr.outcome_index for sr in fix_runs] == [0, 1], fix_runs
        for sr in fix_runs:
            assert _approx(sr.cost_usd, PER_CALL_COST), (
                "each visit's cost is independent (per-visit, not cumulative)",
                sr.cost_usd,
            )
    finally:
        _prov._OVERRIDES.pop("claude", None)
        _prov._OVERRIDES.pop("lqafail", None)
        _reset_ledger()
    print("  (5b) loop: two visits = two stamped rows, costs independent OK")


def test_paused_resume_stamps_both_visits():
    """(6) A run that pauses at a human_review step stamps the pre-pause llm step; after
    resume the post-review llm step stamps too — both honestly."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})
    register_provider("claude", PricedStub())
    try:
        doc_id = _mk_doc(1, prefix="pause")
        wf_id = _new_wf("pause")
        _add_step(wf_id, 0, "Translate")
        _add_step(wf_id, 1, "Review", step_type=StepType.HUMAN_REVIEW, prompt_text="")
        _add_step(wf_id, 2, "Finalize", prompt_text="Finalize: {text}")
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
        assert summary["status"] == "paused", summary
        run_id = summary["run_id"]
        pre = _stepruns(run_id)
        # the pre-pause Translate step stamped its cost; the PAUSED human-review row did
        # not spend -> NULL (honest).
        translate = [sr for sr in pre if sr.status == "completed"]
        assert translate and _approx(translate[0].cost_usd, PER_CALL_COST), pre
        paused = [sr for sr in pre if sr.status == "paused"]
        assert paused and paused[0].cost_usd is None, (
            "a human-review pause spends nothing -> NULL, not 0",
            paused,
        )
        with Session(engine) as s:
            resumed = orchestrator.resume_workflow(s, run_id, "es-ES", "edited target")
        assert resumed["status"] == "completed", resumed
        final = [
            sr
            for sr in _stepruns(run_id)
            if sr.status == "completed" and _approx(sr.cost_usd, PER_CALL_COST)
        ]
        assert len(final) == 2, (
            "both the pre-pause AND post-resume llm steps stamp their own cost",
            [sr.cost_usd for sr in _stepruns(run_id)],
        )
    finally:
        _prov._OVERRIDES.pop("claude", None)
        _reset_ledger()
    print("  (6) paused-then-resumed run stamps BOTH visits honestly OK")


def test_show_run_surfaces_carry_spend():
    """(7) run_overlay (the ONE serializer behind show-run local/remote/api) exposes
    cost_usd/tokens_in/tokens_out in step_states (summed across visits) + visit_history
    (per row); a legacy/untracked row reads NULL."""
    steps = [SimpleNamespace(id=10, key="translate"), SimpleNamespace(id=11, key="lqa")]
    rows = [
        SimpleNamespace(
            id=1,
            step_id=10,
            status="completed",
            score=None,
            gate=None,
            provider_used="claude",
            model_used="m",
            attempts=1,
            error=None,
            latency_ms=5,
            started_at=None,
            finished_at=None,
            outcome_index=0,
            cost_usd=0.006,
            tokens_in=600,
            tokens_out=120,
        ),
        SimpleNamespace(
            id=2,
            step_id=10,
            status="completed",
            score=None,
            gate=None,
            provider_used="claude",
            model_used="m",
            attempts=1,
            error=None,
            latency_ms=5,
            started_at=None,
            finished_at=None,
            outcome_index=1,
            cost_usd=0.004,
            tokens_in=400,
            tokens_out=80,
        ),
        # a legacy/untracked row: cost/tokens NULL
        SimpleNamespace(
            id=3,
            step_id=11,
            status="completed",
            score=None,
            gate=None,
            provider_used="claude",
            model_used="m",
            attempts=1,
            error=None,
            latency_ms=5,
            started_at=None,
            finished_at=None,
            outcome_index=0,
            cost_usd=None,
            tokens_in=None,
            tokens_out=None,
        ),
    ]
    ov = run_overlay.build_overlay(rows, steps)
    by_key = {s["key"]: s for s in ov["step_states"]}
    # step_states: summed across the two translate visits; untracked lqa stays NULL.
    assert _approx(by_key["translate"]["cost_usd"], 0.010), by_key["translate"]
    assert by_key["translate"]["tokens_in"] == 1000 and by_key["translate"]["tokens_out"] == 200
    assert by_key["lqa"]["cost_usd"] is None and by_key["lqa"]["tokens_in"] is None, (
        "an untracked step reads NULL, not 0",
        by_key["lqa"],
    )
    # visit_history: per-row, both translate visits carry their own figures.
    vh = run_overlay.visit_history(rows, steps)
    tvis = [v for v in vh if v["key"] == "translate"]
    assert {v["cost_usd"] for v in tvis} == {0.006, 0.004}, tvis
    assert all("tokens_in" in v and "tokens_out" in v for v in vh)
    lvis = [v for v in vh if v["key"] == "lqa"][0]  # noqa: RUF015
    assert lvis["cost_usd"] is None and lvis["tokens_out"] is None, lvis
    print(
        "  (7) show-run serializer surfaces spend in step_states + visit_history; "
        "legacy rows read NULL OK"
    )


def test_full_text_priced_run_matches_ledger():
    """(9) A FULL_TEXT doc through the orchestrator on a priced provider stamps its
    StepRun cost EXACTLY equal to the ledger delta (the FULL_TEXT accumulation path,
    distinct from the segmented fan-out)."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})
    register_provider("claude", PricedStub())
    try:
        before = spend_svc.spent_this_period()
        doc_id = _mk_full_text_doc(prefix="ftpriced")
        wf_id = _new_wf("ftpriced")
        _add_step(wf_id, 0, "Translate")
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
        assert summary["status"] == "completed", summary
        srs = _stepruns(summary["run_id"])
        assert len(srs) == 1, srs
        sr = srs[0]
        ledger_delta = spend_svc.spent_this_period() - before
        assert sr.cost_usd is not None and sr.cost_usd > 0
        assert _approx(sr.cost_usd, ledger_delta), (
            "FULL_TEXT stamped cost must equal the ledger delta",
            sr.cost_usd,
            ledger_delta,
        )
        assert sr.tokens_in == PER_CALL_TIN and sr.tokens_out == PER_CALL_TOUT, sr
    finally:
        _prov._OVERRIDES.pop("claude", None)
        _reset_ledger()
    print("  (9) FULL_TEXT priced run: stamped cost == ledger delta OK")


def test_qe_judge_soft_skip_keeps_spend():
    """(10) The reviewer's leak: a QE judge call that spends but SOFT-SKIPS must still
    carry its real cost on the returned card (else the step-visit under-stamps + the
    ledger and the workflow-axis diverge). Both spent skip paths:
      * junk_json — a priced judge returns non-JSON (a live-provider refusal);
      * provider_error — a priced call succeeds but returns whitespace-only text.
    Each card's cost_usd must equal the ledger delta of that one call (>0)."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})
    register_provider("junkjudge", PricedJunkJudge())
    register_provider("wsjudge", PricedWhitespaceJudge())
    try:
        # (a) junk_json path
        before = spend_svc.spent_this_period()
        card = qe.score_segment(
            ProviderSpec(provider="junkjudge", model=PRICED_MODEL),
            "hello",
            "hola",
            "en-US",
            "es-ES",
        )
        delta = spend_svc.spent_this_period() - before
        assert card["skipped"] and card["reason"] == "junk_json", card
        assert delta > 0, "the priced junk judge must have charged the ledger"
        assert _approx(card["cost_usd"], delta), (
            "junk_json skip must carry the judge's real cost (the leak)",
            card["cost_usd"],
            delta,
        )
        assert card["tokens_in"] == PER_CALL_TIN and card["tokens_out"] == PER_CALL_TOUT, card

        # (b) provider_error (whitespace-only text) path — priced, charged, must carry cost
        before = spend_svc.spent_this_period()
        card2 = qe.score_segment(
            ProviderSpec(provider="wsjudge", model=PRICED_MODEL), "hello", "hola", "en-US", "es-ES"
        )
        delta2 = spend_svc.spent_this_period() - before
        assert card2["skipped"] and card2["reason"] == "provider_error", card2
        assert delta2 > 0, "a priced whitespace-only call still charges the ledger"
        assert _approx(card2["cost_usd"], delta2), (
            "whitespace-only provider_error skip must carry its real cost",
            card2["cost_usd"],
            delta2,
        )
    finally:
        _prov._OVERRIDES.pop("junkjudge", None)
        _prov._OVERRIDES.pop("wsjudge", None)
        _reset_ledger()
    print(
        "  (10) QE soft-skips (junk_json + whitespace provider_error) carry their "
        "real judge spend — no dropped cost OK"
    )


def main():
    init_db()
    settings.set_many({"spend_cap_usd": "0"})
    test_migration_columns_and_legacy_null()
    test_priced_run_stamps_exact_and_matches_ledger()
    test_mock_run_honest_zeros_and_no_accrual()
    test_parallel_segments_accumulate_exactly()
    test_two_steps_independent_costs()
    test_loop_two_visits_independent_costs()
    test_paused_resume_stamps_both_visits()
    test_show_run_surfaces_carry_spend()
    test_full_text_priced_run_matches_ledger()
    test_qe_judge_soft_skip_keeps_spend()
    settings.set_many({"spend_cap_usd": "0"})
    print("WORKFLOW SPEND OK")


if __name__ == "__main__":
    main()
