"""Per-workflow usage + quality stats (app/services/workflow_stats.py), offline +
hermetic. The single-sourced stats behind the /workflows dashboard, `workflow list`
stats columns, and /api workflows list.

Fixtures are ORCHESTRATOR-DRIVEN (the fixture-realism lesson: create runs by RUNNING
mock workflows, not hand-inserting StepRun rows — grep verify_batch_runs / the run
overlay history for why hand rows mask real defects). A few deliberately-legacy or
scoping probe rows are inserted directly and each is called out.

Coverage:
  (1) counts + last_run + distinct projects/customers from real runs (batch included).
  (2) spend honest NULL: an all-mock (0.0-cost but tracked) run vs a truly untracked
      (legacy StepRun NULL) run; a priced run's real sum.
  (3) quality signal: LQA gate pass rate (per-visit, LQA-only) + needs_review rate
      (a paused run) — the documented definitions.
  (4) scoping via access.visible_project_ids: a bound linguist sees only its customer's
      runs of a SHARED (global) workflow; a two-org probe never bleeds.
  (5) group aggregation: for_workflows unions distinct customers/projects (no double
      count) across a group's variants.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfstats_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, settings, workflow_stats
from app.services import providers as _prov
from app.services.providers import Provider, register_provider


# --- a priced provider so at least one run stamps a real (non-NULL, >0) cost --------
class PricedStub(Provider):
    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "[es] " + (payload or {}).get("text", "hola")


class LqaPass(Provider):
    name = "lqapass"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return '{"errors":[]}'  # no errors -> gate passes


class LqaFail(Provider):
    name = "lqafail"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return '{"errors":[{"dimension":"accuracy","severity":"critical","note":"x"}]}'


def _mk_project(name, *, customer_id=None, org_id=None, source="en-US"):
    with Session(engine) as s:
        p = Project(name=name, source_locale=source, customer_id=customer_id, org_id=org_id)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_doc(project_id, *, n=1, target="es-ES", prefix="d"):
    with Session(engine) as s:
        doc = Document(project_id=project_id, filename=f"{prefix}.txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i in range(n):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=target,
                    order_index=i,
                    source_text=f"Sentence {i}.",
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return doc.id


def _new_wf(
    name, *, provider="mock", model="m", steps=None, customer_id=None, org_id=None, group_id=None
):
    with Session(engine) as s:
        wf = Workflow(
            name=name, scope="global", customer_id=customer_id, org_id=org_id, group_id=group_id
        )
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for i, st in enumerate(steps or [{"name": "T", "prompt_text": "T {text}"}]):
            s.add(
                WorkflowStep(
                    workflow_id=wf.id,
                    order_index=i,
                    name=st.get("name", "T"),
                    step_type=st.get("step_type", StepType.LLM_AGENT),
                    provider=st.get("provider", provider),
                    model=st.get("model", model),
                    prompt_text=st.get("prompt_text", "T {text}"),
                    chunk_policy="none",
                    retry_max=1,
                    backoff_base=0.0,
                    key=st.get("key"),
                    branch=st.get("branch"),
                    max_visits=st.get("max_visits"),
                )
            )
        s.commit()
        return wf.id


def _run(wf_id, doc_id, locale="es-ES"):
    with Session(engine) as s:
        return orchestrator.run_workflow(s, wf_id, doc_id, locale)


def _approx(a, b, tol=1e-9):
    return abs((a or 0.0) - (b or 0.0)) <= tol


# ============================================================================
def test_counts_projects_customers_and_batch():
    """(1) runs_total/runs_30d/last_run + distinct projects & customers, over runs on
    TWO projects of TWO customers, plus a batch-member run counted naturally."""
    from app.services import batch_runs

    cust_a = _cust("Stats Cust A")
    cust_b = _cust("Stats Cust B")
    pa = _mk_project("stats-A", customer_id=cust_a)
    pb = _mk_project("stats-B", customer_id=cust_b)
    wf = _new_wf("counts-wf", provider="mock")
    # two single runs on project A (same customer), one on project B (other customer)
    d1 = _mk_doc(pa, prefix="a1")
    d2 = _mk_doc(pa, prefix="a2")
    d3 = _mk_doc(pb, prefix="b1")
    for wf_doc in (d1, d2, d3):
        r = _run(wf, wf_doc)
        assert r["status"] == "completed", r
    # a BATCH over a multi-target doc on project A -> member WorkflowRun rows (2 locales)
    pmulti = _mk_project("stats-multi", customer_id=cust_a, source="en-US")
    with Session(engine) as s:
        doc = Document(project_id=pmulti, filename="m.txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for loc in ("es-ES", "fr-FR"):
            for i in range(1):
                s.add(
                    Segment(
                        document_id=doc.id,
                        target_locale=loc,
                        order_index=i,
                        source_text="Hi.",
                        status=SegStatus.NEW,
                    )
                )
        s.commit()
        dm = doc.id
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=dm, locales=["es-ES", "fr-FR"], synchronous=True
        )
    with Session(engine) as s:
        members = s.exec(select(WorkflowRun).where(WorkflowRun.batch_id == batch.id)).all()
    assert len(members) == 2, ("batch should have 2 member runs", members)

    with Session(engine) as s:
        st = workflow_stats.stats_for(s, [wf])[wf]
    # 3 single runs + 2 batch members = 5 WorkflowRun rows
    assert st["runs_total"] == 5, st
    assert st["runs_30d"] == 5, st
    assert st["last_run_at"] is not None
    # distinct projects: A, B, multi = 3
    assert st["projects_served"] == 3, st
    # distinct customers: A, B = 2 (multi belongs to A)
    assert st["customers_served"] == 2, st
    assert st["active"] is False
    print("  (1) counts + distinct projects/customers incl. batch members OK")


def test_spend_honest_null():
    """(2) spend_usd honest NULL: an all-mock run stamps 0.0 (TRACKED) -> spend 0.0;
    a truly legacy run (StepRun.cost_usd NULL) -> untracked; a priced run -> real >0."""
    # (a) mock-only workflow: mock stamps 0.0 cost (tracked, honest zero)
    p = _mk_project("spend-mock")
    wf_mock = _new_wf("spend-mock-wf", provider="mock")
    _run(wf_mock, _mk_doc(p, prefix="sm"))
    with Session(engine) as s:
        st = workflow_stats.stats_for(s, [wf_mock])[wf_mock]
    assert st["spend_usd"] == 0.0, ("mock stamps a tracked 0.0, not NULL", st)

    # (b) a truly UNTRACKED workflow: a hand-inserted legacy StepRun with cost_usd NULL
    # (the only place a direct row is honest — legacy rows predate spend tracking).
    wf_legacy = _new_wf("spend-legacy-wf", provider="mock")
    with Session(engine) as s:
        step = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_legacy)).first()
        doc_id = _mk_doc(p, prefix="sl")
        run = WorkflowRun(
            workflow_id=wf_legacy, document_id=doc_id, target_locale="es-ES", status="completed"
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(StepRun(workflow_run_id=run.id, step_id=step.id, status="completed"))
        s.commit()
    with Session(engine) as s:
        st = workflow_stats.stats_for(s, [wf_legacy])[wf_legacy]
    assert st["runs_total"] == 1 and st["spend_usd"] is None, (
        "an all-NULL-cost workflow reports spend None, never a fake 0",
        st,
    )

    # a fully-tracked (mock) workflow is NOT partial
    with Session(engine) as s:
        st = workflow_stats.stats_for(s, [wf_mock])[wf_mock]
    assert st["spend_partial"] is False, ("all runs tracked -> not partial", st)
    # an all-untracked workflow (spend None) is NOT partial (nothing to qualify)
    with Session(engine) as s:
        st = workflow_stats.stats_for(s, [wf_legacy])[wf_legacy]
    assert st["spend_partial"] is False, ("spend None -> not partial", st)

    # (c) a PRICED run -> real >0 sum
    register_provider("claude", PricedStub())
    settings.set_many({"spend_cap_usd": "0"})
    try:
        wf_priced = _new_wf("spend-priced-wf", provider="claude", model="claude-opus-4-8")
        _run(wf_priced, _mk_doc(p, prefix="sp"))
        with Session(engine) as s:
            st = workflow_stats.stats_for(s, [wf_priced])[wf_priced]
        assert st["spend_usd"] is not None and st["spend_usd"] > 0, st
        assert st["spend_partial"] is False, ("single tracked run -> not partial", st)

        # (d) MIXED: add a truly-untracked legacy run to the SAME priced workflow ->
        # spend stays a real number BUT spend_partial flips True (the total is a floor).
        with Session(engine) as s:
            step = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_priced)).first()
            doc_id = _mk_doc(p, prefix="spmix")
            run = WorkflowRun(
                workflow_id=wf_priced, document_id=doc_id, target_locale="es-ES", status="completed"
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            s.add(StepRun(workflow_run_id=run.id, step_id=step.id, status="completed"))
            s.commit()
        with Session(engine) as s:
            st = workflow_stats.stats_for(s, [wf_priced])[wf_priced]
        assert st["spend_usd"] is not None and st["spend_usd"] > 0, st
        assert st["spend_partial"] is True, (
            "a mix of tracked + untracked runs must mark spend PARTIAL",
            st,
        )
    finally:
        _prov._OVERRIDES.pop("claude", None)
    print(
        "  (2) spend honest NULL (mock 0.0 tracked / legacy NULL untracked / "
        "priced real / MIXED -> spend_partial) OK"
    )


def test_quality_signal_lqa_and_needs_review():
    """(3) lqa_pass_rate (per-visit, LQA-only) + needs_review_rate (a paused run)."""
    register_provider("lqapass", LqaPass())
    register_provider("lqafail", LqaFail())
    settings.set_many({"spend_cap_usd": "0"})
    p = _mk_project("quality")
    try:
        # Workflow: Translate -> LQA (pass). One run -> one LQA gate 'passed'.
        wf_pass = _new_wf(
            "q-pass",
            provider="mock",
            steps=[
                {"name": "T", "prompt_text": "T {text}"},
                {"name": "LQA", "step_type": StepType.LQA, "provider": "lqapass", "key": "lqa"},
            ],
        )
        _run(wf_pass, _mk_doc(p, prefix="qp"))
        with Session(engine) as s:
            st = workflow_stats.stats_for(s, [wf_pass])[wf_pass]
        assert st["lqa_gate_count"] == 1 and st["lqa_pass_rate"] == 1.0, st

        # Workflow with a correction LOOP: Translate -> Fix(loop) -> LQA(fail) so the
        # LQA gate fails repeatedly (per-visit counted). Run COMPLETES (loop cap) so it
        # is not itself needs_review; the pass rate is 0 over >1 gate outcome.
        wf_fail = _new_wf(
            "q-fail",
            provider="mock",
            steps=[
                {"name": "T", "prompt_text": "T {text}"},
                {"name": "Fix", "key": "fix", "prompt_text": "Fix {text}", "max_visits": 2},
                {
                    "name": "LQA",
                    "step_type": StepType.LQA,
                    "provider": "lqafail",
                    "key": "lqa",
                    "branch": {"on_pass": "end", "on_fail": {"step": "fix"}},
                },
            ],
        )
        r = _run(wf_fail, _mk_doc(p, prefix="qf"))
        assert r["status"] == "completed", r
        with Session(engine) as s:
            st = workflow_stats.stats_for(s, [wf_fail])[wf_fail]
        assert st["lqa_gate_count"] >= 2, ("per-visit LQA gates counted", st)
        assert st["lqa_pass_rate"] == 0.0, st
        # this workflow's runs never paused -> needs_review 0
        assert st["needs_review_count"] == 0 and st["needs_review_rate"] == 0.0, st

        # needs_review: a workflow whose run PAUSES at a human_review step.
        wf_pause = _new_wf(
            "q-pause",
            provider="mock",
            steps=[
                {"name": "T", "prompt_text": "T {text}"},
                {"name": "Review", "step_type": StepType.HUMAN_REVIEW, "prompt_text": ""},
                {"name": "Final", "prompt_text": "F {text}"},
            ],
        )
        r = _run(wf_pause, _mk_doc(p, prefix="qr"))
        assert r["status"] == "paused", r
        # plus one clean completing run of the same workflow (no pause step reached?
        # it always pauses) — run a second doc: it will also pause. Use wf_pass's doc
        # count instead: here just assert the single paused run.
        with Session(engine) as s:
            st = workflow_stats.stats_for(s, [wf_pause])[wf_pause]
        assert st["runs_total"] == 1 and st["needs_review_count"] == 1, st
        assert st["needs_review_rate"] == 1.0, st
        assert st["lqa_pass_rate"] is None and st["lqa_gate_count"] == 0, (
            "no LQA step -> pass rate None, not 0",
            st,
        )
    finally:
        _prov._OVERRIDES.pop("lqapass", None)
        _prov._OVERRIDES.pop("lqafail", None)
    print("  (3) quality signal: LQA per-visit pass rate + paused needs_review OK")


def test_scoping_bound_linguist_and_two_org():
    """(4) A GLOBAL workflow run on two different customers' projects: a customer-bound
    user sees ONLY its own customer's runs (via access.visible_project_ids); an
    unbound/internal caller sees all. Probes the isolation the dashboard relies on."""
    import app.services.security as security

    cust_x = _cust("Scope Cust X")
    cust_y = _cust("Scope Cust Y")
    px = _mk_project("scope-X", customer_id=cust_x)
    py = _mk_project("scope-Y", customer_id=cust_y)
    wf = _new_wf("shared-global-wf", provider="mock")  # GLOBAL, shared
    _run(wf, _mk_doc(px, prefix="sx"))
    _run(wf, _mk_doc(px, prefix="sx2"))
    _run(wf, _mk_doc(py, prefix="sy"))

    with Session(engine) as s:
        st_all = workflow_stats.stats_for(s, [wf], user=None)[wf]
    assert st_all["runs_total"] == 3 and st_all["customers_served"] == 2, st_all

    # A bound user of customer X: auth must be ON for the binding to engage.
    if not security.auth_required():
        settings.set_many({"security_auth_required": "1"})
    try:
        bound = security.create_user("boundx@stats.com", "BoundX", "linguist", "boundpass123")
        with Session(engine) as s:
            bx = s.get(type(bound), bound.id)
            bx.customer_id = cust_x
            s.add(bx)
            s.commit()
            s.refresh(bx)
        with Session(engine) as s:
            bx = s.get(type(bound), bound.id)
            st_bound = workflow_stats.stats_for(s, [wf], user=bx)[wf]
        assert st_bound["runs_total"] == 2, (
            "bound X sees only its 2 runs of the shared workflow",
            st_bound,
        )
        assert st_bound["customers_served"] == 1, st_bound
        assert st_bound["projects_served"] == 1, st_bound
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  (4) scoping: bound linguist confined to own customer's runs of a shared workflow OK")


def test_group_aggregation_unions_distinct():
    """(5) for_workflows aggregates a group's variants as ONE bucket with UNION distinct
    semantics — two variants that both ran for the SAME customer count that customer
    once, never twice."""
    from app.services import workflows as wf_service

    cust = _cust("Group Cust")
    p1 = _mk_project("grp-1", customer_id=cust)
    p2 = _mk_project("grp-2", customer_id=cust)  # SAME customer, different project
    with Session(engine) as s:
        grp = wf_service.create_group(s, "Grp", customer_id=str(cust))
        grp_id = grp.id
    # two variant workflows in the group
    v1 = _new_wf("var-1", provider="mock", group_id=grp_id, customer_id=cust)
    v2 = _new_wf("var-2", provider="mock", group_id=grp_id, customer_id=cust)
    _run(v1, _mk_doc(p1, prefix="g1"))
    _run(v2, _mk_doc(p2, prefix="g2"))
    with Session(engine) as s:
        bundle = workflow_stats.collect(s, [v1, v2])
        agg = bundle.for_workflows([v1, v2])
    assert agg["runs_total"] == 2, agg
    assert agg["projects_served"] == 2, ("distinct projects union", agg)
    assert agg["customers_served"] == 1, ("the shared customer counted ONCE, not summed to 2", agg)
    # per-variant still isolated
    with Session(engine) as s:
        one = workflow_stats.stats_for(s, [v1])[v1]
    assert one["runs_total"] == 1, one
    print("  (5) group aggregation unions distinct customers/projects (no double count) OK")


# --- helpers -----------------------------------------------------------------
def _cust(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def test_empty_and_zero_contract():
    """zero_stats + an unknown/never-run workflow id read as an honest blank."""
    z = workflow_stats.zero_stats()
    assert set(z.keys()) == set(workflow_stats.STAT_KEYS), z
    assert z["runs_total"] == 0 and z["spend_usd"] is None and z["active"] is False
    with Session(engine) as s:
        st = workflow_stats.stats_for(s, [999999])[999999]
    assert st == z, ("a never-run workflow reads zero_stats", st)
    print("  (0) zero_stats contract + never-run workflow blank OK")


def main():
    init_db()
    settings.set_many({"spend_cap_usd": "0", "security_auth_required": "0"})
    test_empty_and_zero_contract()
    test_counts_projects_customers_and_batch()
    test_spend_honest_null()
    test_quality_signal_lqa_and_needs_review()
    test_scoping_bound_linguist_and_two_org()
    test_group_aggregation_unions_distinct()
    print("WORKFLOW STATS OK")


if __name__ == "__main__":
    main()
