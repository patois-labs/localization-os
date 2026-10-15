"""#364: every Job stores the exact USD the live ledger booked while it ran.

Reports, the project budget meter and the project_check refusal prefer that stored figure
(Job.cost_usd) over re-pricing the job's tokens by the engine's CURRENT profile. A job with no
stored figure (older rows, a cut-off live stream) still prices as before.
Hermetic and offline: stub providers only. Run from tests/verify_spend_cap.py (run()).
"""

import threading
from datetime import datetime
from unittest.mock import patch

from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import Document, Job, JobStatus, Project, Segment
from app.services import ai_optout, engines, jobs, project_budgets, providers, reports, spend

PRIVATE = "http://127.0.0.1:1234/v1"
PUBLIC = "http://203.0.113.9:1234/v1"
M = 1_000_000
USAGE = {"input_tokens": M, "output_tokens": M}
QWEN = "qwen/qwen3-27b"  # no pricing row: a priced call lands on the 5/25 default


class FreeLocal(providers.LocalProvider):
    """A self-hosted engine on a private endpoint: the ledger books $0."""

    def __init__(self):
        self._profile_base = PRIVATE

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = dict(USAGE)
        return "bonjour"


class PaidStub:
    """A paid provider: not a Mock/Local instance, so the ledger prices it by name."""

    name = "claude"
    last_usage = None

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = dict(USAGE)
        return "salut"


def _project(name, default_provider="mock"):
    with Session(engine) as s:
        p = Project(name=name, default_provider=default_provider)
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.docx", fmt="docx")
        s.add(d)
        s.commit()
        s.refresh(d)
        for i in range(3):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="fr-FR",
                    order_index=i,
                    source_text=f"Hello number {i} from the cost test",
                )
            )
        s.commit()
        return p.id, d.id


def _job(did, provider, cost=None, tokens=M, when=None):
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            payload={"document_id": did, "locale": "fr-FR"},
            status=JobStatus.COMPLETED,
            result={"provider_used": {provider: 1}, "tokens_in": tokens, "tokens_out": tokens},
            created_by="u@x.co",
            created_at=when or datetime.utcnow(),
            finished_at=when or datetime.utcnow(),
        )
        j.cost_usd = cost
        s.add(j)
        s.commit()
        s.refresh(j)
        return j


def _fresh(jid):
    with Session(engine) as s:
        return s.get(Job, jid)


def _handler(fn):
    """Swap the translate_document handler for the duration of one run_sync."""
    return patch.dict(jobs.HANDLERS, {"translate_document": fn})


def sink_semantics():
    """The ledger entry points feed every active sink; nested scopes both see the spend."""
    with spend.cost_scope() as outer:
        spend.record_external_usd(0.25)
        with spend.cost_scope() as inner:
            spend.record_usage("claude", config.DEFAULT_MODELS["claude"], USAGE)
        assert inner.total > 0
        assert abs(outer.total - (0.25 + inner.total)) < 1e-9, (outer.total, inner.total)
    # outside any scope nothing is collected and nothing raises
    spend.record_external_usd(0.01)
    # a free call books nothing
    with spend.cost_scope() as sink:
        spend.record_provider(FreeLocal(), "local", QWEN, prompt="a", output="b")
    assert sink.total == 0.0, sink.total
    print("  #364: cost scopes collect exactly what the ledger booked (nested, free = 0)")


def free_job_survives_engine_delete():
    """The headline bug: a free engine's job re-priced at 5/25 once the engine is deleted."""
    eng = engines.create_profile("f364-free", "local", base_url=PRIVATE, model=QWEN)
    ref = f"engine:{eng.id}"
    _pid, did = _project("f364-free", default_provider=ref)
    with (
        patch.object(config, "LOCAL_BASE_URL", PRIVATE),
        patch.object(providers, "get_provider", return_value=FreeLocal()),
    ):
        job = jobs.run_sync("translate_document", {"document_id": did, "locale": "fr-FR"})
    job = _fresh(job.id)
    assert job.status == JobStatus.COMPLETED, (job.status, job.error)
    assert job.cost_usd == 0.0, job.cost_usd
    assert "cost_usd" not in (job.result or {}), "the figure lives in the column, not the result"
    assert reports._job_cost(job) == 0.0
    with Session(engine) as s:  # an engine in use cannot be deleted: clear the project default
        proj = s.get(Project, s.get(Document, did).project_id)
        proj.default_provider = "mock"
        s.add(proj)
        s.commit()
    assert engines.delete_profile(eng.id)
    engines._profile_memo.clear()
    assert reports._job_cost(_fresh(job.id)) == 0.0, "a deleted free engine's old job stays $0"
    # an older row with the same shape and no stored figure still prices as before
    old = _job(did, ref, cost=None)
    assert reports._job_cost(old) > 0.0, (
        "no stored cost: today's pricing (deleted engine = default)"
    )
    print("  #364: free engine job stays $0 after the engine is deleted; old rows still priced")


def repoint_does_not_rewrite_history():
    eng = engines.create_profile("f364-repoint", "local", base_url=PRIVATE, model=QWEN)
    ref = f"engine:{eng.id}"
    _pid, did = _project("f364-repoint", default_provider=ref)
    with (
        patch.object(config, "LOCAL_BASE_URL", PRIVATE),
        patch.object(providers, "get_provider", return_value=FreeLocal()),
    ):
        job = jobs.run_sync("translate_document", {"document_id": did, "locale": "fr-FR"})
    jid = job.id
    unstored = _job(did, ref, cost=None)
    assert reports._job_cost(unstored) == 0.0
    engines.update_profile(eng.id, base_url=PUBLIC)
    engines._profile_memo.clear()
    assert reports._job_cost(_fresh(jid)) == 0.0, "re-pointing a free engine must not reprice"
    assert reports._job_cost(unstored) > 0.0, "an unstored row follows the engine (as before)"
    print("  #364: re-pointing an engine free -> public leaves the stored job at $0")


def failover_stores_the_paid_share():
    """A free engine that fails over to a paid one: the stored cost is what the ledger booked
    for the paid call, whatever provider the job's tokens say dominated."""
    _pid, did = _project("f364-failover")
    want = spend.cost_for_ref("claude", config.DEFAULT_MODELS["claude"], USAGE)
    assert want > 0

    def handler(payload, progress):
        spec = providers.ProviderSpec(
            "local",
            model=QWEN,
            fallback_provider="claude",
            fallback_model=config.DEFAULT_MODELS["claude"],
            backoff_base=0,
        )

        class _Down(FreeLocal):
            def complete(self, *a, **k):
                self.last_usage = None
                raise RuntimeError("401 invalid api key")

        def pick(name):
            return _Down() if name == "local" else PaidStub()

        with patch.object(providers, "get_provider", side_effect=pick):
            res = providers.run_completion(spec, "hello")
        assert res["text"], res
        # the job's own token total says "the free engine did it" (dominant provider is local)
        return {
            "provider_used": {"local": 9, "claude": 1},
            "tokens_in": M,
            "tokens_out": M,
            "cost_usd": 12345.0,  # a handler-supplied figure must NOT be trusted or stored
        }

    with _handler(handler), patch.object(config, "LOCAL_BASE_URL", PRIVATE):
        job = jobs.run_sync("translate_document", {"document_id": did, "locale": "fr-FR"})
    job = _fresh(job.id)
    assert job.status == JobStatus.COMPLETED, (job.status, job.error)
    assert job.cost_usd is not None and abs(job.cost_usd - want) < 1e-9, (job.cost_usd, want)
    assert "cost_usd" not in job.result, job.result
    assert abs(reports._job_cost(job) - want) < 1e-9
    print("  #364: failover job stores the paid share the ledger booked")


def failed_job_keeps_partial_spend():
    _pid, did = _project("f364-partial")

    def handler(payload, progress):
        spend.record_external_usd(0.4)
        raise RuntimeError("boom after paying")

    with _handler(handler):
        job = jobs.run_sync("translate_document", {"document_id": did, "locale": "fr-FR"})
    job = _fresh(job.id)
    assert job.status == JobStatus.FAILED and abs(job.cost_usd - 0.4) < 1e-9, job.cost_usd

    # a job that failed before any call stores an exact zero, not NULL
    def nothing(payload, progress):
        raise jobs.MissingPayloadField("x")

    with _handler(nothing):
        job2 = _fresh(jobs.run_sync("translate_document", {"document_id": did}).id)
    assert job2.cost_usd == 0.0, job2.cost_usd
    print("  #364: failed jobs keep their partial spend; no-call failures store 0.0")


def concurrent_jobs_and_threads_do_not_mix():
    """Two jobs at once, each fanning out to a pool through ai_optout.submit (the repo's one
    thread-fan-out seam): each stores only its own spend."""
    from concurrent.futures import ThreadPoolExecutor

    _pid, did = _project("f364-conc")
    barrier = threading.Barrier(2)

    def handler(payload, progress):
        amount = float(payload["amount"])
        barrier.wait(timeout=10)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = [ai_optout.submit(pool, spend.record_external_usd, amount / 3) for _ in range(3)]
            for f in futs:
                f.result()
        return {"ok": True}

    out = {}

    def go(amount):
        out[amount] = jobs.run_sync(
            "translate_document", {"document_id": did, "locale": "fr-FR", "amount": amount}
        ).id

    with _handler(handler):
        ts = [threading.Thread(target=go, args=(a,)) for a in (0.3, 0.6)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
    for amount, jid in out.items():
        got = _fresh(jid).cost_usd
        assert got is not None and abs(got - amount) < 1e-9, (amount, got)
    print("  #364: concurrent jobs and their pool threads each store only their own spend")


def budget_and_report_prefer_stored():
    # stored figure beats tokens: tiny tokens on claude but a stored $5 -> a $1 budget refuses
    pid, did = _project("f364-budget-stored")
    _job(did, "claude", cost=5.0, tokens=10)
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, pid, 1.0)
    ok, _r, _m = project_budgets.project_check(pid)
    assert not ok, "the refusal check must read the stored cost"
    # stored $0 beats tokens: a million tokens on claude with a stored zero never refuses
    pid, did = _project("f364-budget-zero")
    _job(did, "claude", cost=0.0, tokens=M)
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, pid, 0.01)
        sp = project_budgets.project_spend_this_month(s, pid)
    assert sp["ai_usd"] == 0.0, sp
    assert project_budgets.project_check(pid)[0]
    # the report reconciles with the meter and never double counts
    pid, did = _project("f364-report")
    _job(did, "claude", cost=2.5, tokens=M)
    _job(did, "claude", cost=None, tokens=M)
    legacy = reports._job_cost(_job(did, "claude", cost=None, tokens=M))
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="none", project_id=pid), user=None
        )
        sp = project_budgets.project_spend_this_month(s, pid)
    want = 2.5 + 2 * legacy
    assert abs(rep["rows"][0]["cost_usd"] - want) < 1e-6, (rep["rows"], want)
    assert abs(sp["ai_usd"] - want) < 1e-6, (sp, want)
    # junk stored values never poison a report: negative / NaN fall back to pricing
    for bad in (-1.0, float("nan")):
        j = _job(did, "claude", cost=bad, tokens=M)
        assert abs(reports._job_cost(j) - legacy) < 1e-9, (bad, reports._job_cost(j))
    print("  #364: reports + project meter + refusal prefer the stored cost; old rows fall back")


def sync_and_live_paths():
    _pid, did = _project("f364-sync")
    job = jobs.record_sync_job(
        "translate_document",
        {"document_id": did, "locale": "fr-FR"},
        result={"provider_used": {"claude": 1}, "tokens_in": 1, "tokens_out": 1, "cost_usd": 1.75},
        total=1,
    )
    job = _fresh(job.id)
    assert job.cost_usd == 1.75 and "cost_usd" not in job.result, (job.cost_usd, job.result)
    plain = _fresh(
        jobs.record_sync_job("translate_document", {"document_id": did}, result={}, total=0).id
    )
    assert plain.cost_usd is None, "a sync row with no figure stays unknown"

    # live stream: a settled stream stores the stream's own figure; a cut-off keeps NULL
    stream_id = _live_job(did)
    jobs.settle_live_translate(
        stream_id,
        {"provider_used": {"claude": 1}, "tokens_in": 5, "tokens_out": 5, "cost_usd": 0.5},
        done=1,
    )
    s1 = _fresh(stream_id)
    assert s1.status == JobStatus.COMPLETED and s1.cost_usd == 0.5, (s1.status, s1.cost_usd)
    assert "cost_usd" not in s1.result
    cut = _live_job(did)
    jobs.settle_live_translate(
        cut, None, done=0, error="cut", partial={"tokens_in": 9, "tokens_out": 9, "cost_usd": 0.1}
    )
    s2 = _fresh(cut)
    assert s2.status == JobStatus.FAILED and s2.cost_usd is None, s2.cost_usd
    assert "cost_usd" not in (s2.result or {})
    print("  #364: CLI sync rows and live streams store the figure; a cut-off stays estimated")


def _live_job(did):
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            payload={"document_id": did, "locale": "fr-FR", "live_stream": True},
            status=JobStatus.RUNNING,
            result={"provider_used": {"claude": 1}, "tokens_in": 1, "tokens_out": 1},
            created_by="u@x.co",
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def stream_summary_carries_cost():
    """pretranslate_stream puts the ledger figure on the caller's running sink ONLY: the
    summary event reaches translator-role surfaces (live SSE, image API, CLI JSON) and AI
    spend is manager+ data (gate fix)."""
    from app.services import translate

    _pid, did = _project("f364-stream", default_provider="claude")
    running: dict = {}
    summary = None
    with Session(engine) as s, patch.object(providers, "get_provider", return_value=PaidStub()):
        for ev in translate.pretranslate_stream(s, did, "fr-FR", stats_sink=running):
            assert "cost_usd" not in ev, ev
            if ev.get("type") == "summary":
                summary = ev
    assert summary is not None
    calls = summary["provider_calls"]
    assert calls >= 1, summary
    want = calls * spend.cost_for_ref("claude", config.DEFAULT_MODELS["claude"], USAGE)
    assert abs(running["cost_usd"] - want) < 1e-9, (running.get("cost_usd"), want, calls)
    print("  #364: pretranslate_stream running totals carry the booked cost; no event does")


def cli_doc_translate_stores_cost():
    """End to end: `doc translate` (the sync CLI path) stores the stream's booked USD on its
    Job row, and its JSON output does not carry the figure."""
    import io
    import json
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as cli_main

    _pid, did = _project("f364-cli", default_provider="claude")
    with Session(engine) as s:
        before = {j.id for j in s.exec(select(Job)).all()}
    out, err = io.StringIO(), io.StringIO()
    with (
        patch.object(providers, "get_provider", return_value=PaidStub()),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        try:
            code = cli_main(["--json", "doc", "translate", str(did), "--locale", "fr-FR"])
        except SystemExit as exc:
            code = exc.code
    assert code in (0, None), (code, out.getvalue(), err.getvalue())
    assert "cost_usd" not in out.getvalue(), out.getvalue()
    stats = json.loads(out.getvalue().strip().splitlines()[-1]).get("stats") or {}
    with Session(engine) as s:
        rows = [j for j in s.exec(select(Job)).all() if j.id not in before]
    assert len(rows) == 1, [(j.id, j.kind) for j in rows]
    calls = stats.get("provider_calls", 0)
    assert calls >= 1, stats
    want = calls * spend.cost_for_ref("claude", config.DEFAULT_MODELS["claude"], USAGE)
    assert rows[0].cost_usd is not None and abs(rows[0].cost_usd - want) < 1e-9, (
        rows[0].cost_usd,
        want,
    )
    assert "cost_usd" not in (rows[0].result or {})
    print("  #364: CLI doc translate stores the booked cost on its Job row, not in its output")


def backfill_meter_reconciles():
    """Gate fix: a project-scoped backfill_embeddings Job's stored cost is counted by the
    cost report, so the project meter counts it too (they reconcile)."""
    pid, _did = _project("f364-backfill")
    with Session(engine) as s:
        j = Job(
            kind="backfill_embeddings",
            payload={"project_id": pid},
            status=JobStatus.COMPLETED,
            result={"embedded": 3, "tokens": 300},
            created_by="u@x.co",
            finished_at=datetime.utcnow(),
        )
        j.cost_usd = 0.7
        s.add(j)
        s.commit()
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="none", project_id=pid), user=None
        )
        sp = project_budgets.project_spend_this_month(s, pid)
    assert abs(rep["rows"][0]["cost_usd"] - 0.7) < 1e-9, rep["rows"]
    assert abs(sp["ai_usd"] - 0.7) < 1e-9, sp
    print("  #364: a project backfill's stored cost is on the report AND the project meter")


def not_user_settable():
    """No door takes the figure: a hostile payload key is inert and the column is only written
    by the job runner / settle / sync recorder."""
    _pid, did = _project("f364-hostile")

    def handler(payload, progress):
        return {"ok": True}

    with _handler(handler):
        job = jobs.run_sync(
            "translate_document",
            {"document_id": did, "locale": "fr-FR", "cost_usd": 999, "result": {"cost_usd": 999}},
        )
    assert _fresh(job.id).cost_usd == 0.0
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app"
    writers = []
    for p in root.rglob("*.py"):
        text = p.read_bytes().decode("utf-8", "replace")
        if re.search(r"Job\([^)]*cost_usd\s*=", text) or "job.cost_usd =" in text:
            writers.append(p.name)
    assert set(writers) <= {"jobs.py"}, writers
    print("  #364: no API/CLI/web door accepts the stored cost; only the job runner writes it")


def no_raw_pool_fanout():
    """Gate guard: a worker-pool task books spend into the job's sink only because
    ai_optout.submit runs it in a COPY of the submitter's context (a ThreadPoolExecutor
    does not carry contextvars). A raw pool.submit / run_in_executor would under-store
    Job.cost_usd (the budget-bypass direction) and also drop the AI opt-out scope, so app/
    has none outside the known spend-free seams."""
    import re
    from pathlib import Path

    allowed = {
        ("ai_optout.py", "pool.submit(contextvars.copy_context().run"),  # the seam itself
        ("health.py", "_probe_pool.submit(db_ready)"),  # readiness probe: no engine calls
        ("vendors.py", "getElementById('sso').submit()"),  # an HTML form, not a pool
        (
            "notify.py",
            "_POOL.submit(deliver_ids, row_ids)",
        ),  # sends queued messages: no engine calls
    }
    root = Path(__file__).resolve().parent.parent / "app"
    bad = []
    for p in root.rglob("*.py"):
        for n, line in enumerate(p.read_bytes().decode("utf-8", "replace").splitlines(), 1):
            if not re.search(r"\.submit\(|run_in_executor\(", line):
                continue
            if re.search(r"\b_?ai_optout\.submit\(|\b_?jobs\.submit\(", line):
                continue
            if any(p.name == f and frag in line for f, frag in allowed):
                continue
            bad.append(f"{p.relative_to(root)}:{n}: {line.strip()}")
    assert not bad, "raw pool fan-out (use ai_optout.submit):\n" + "\n".join(bad)
    print("  #364: every worker-pool fan-out in app/ carries the job's cost sink")


def run():
    sink_semantics()
    free_job_survives_engine_delete()
    repoint_does_not_rewrite_history()
    failover_stores_the_paid_share()
    failed_job_keeps_partial_spend()
    concurrent_jobs_and_threads_do_not_mix()
    budget_and_report_prefer_stored()
    sync_and_live_paths()
    stream_summary_carries_cost()
    cli_doc_translate_stores_cost()
    backfill_meter_reconciles()
    not_user_settable()
    no_raw_pool_fanout()
    assert select  # imported for parity with sibling case modules
