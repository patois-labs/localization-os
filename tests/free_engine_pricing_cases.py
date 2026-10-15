"""#361: free self-hosted engines are free in EVERY spend figure, not just the live ledger.

One shared rule (spend.kind_is_free / ref_is_free / cost_for_ref / record_ref_usage) serves the
live ledger, reports, project budgets and
the OCR / image-localization spend records. Hermetic and offline: no engine is ever called.
Run from tests/verify_spend_cap.py (run()).
"""

from datetime import datetime
from unittest.mock import patch

from sqlmodel import Session

from app import config
from app.db import engine
from app.models import Document, Job, JobStatus, Project
from app.services import engines, pricing, project_budgets, providers, reports, spend

PRIVATE = "http://127.0.0.1:1234/v1"
PUBLIC = "http://203.0.113.9:1234/v1"  # documentation range: not loopback/private/tailnet
M = 1_000_000
USAGE = {"input_tokens": M, "output_tokens": M}
QWEN = "qwen/qwen3-27b"  # matches no pricing row, so a priced call lands on the 5/25 default


def _engines():
    """One engine of every shape the rule must tell apart."""
    mk = engines.create_profile
    ids = {
        "m5": mk("f361-m5-router", "local", base_url=PRIVATE, model=QWEN).id,
        "tailnet": mk("f361-tailnet", "local", base_url="http://100.64.1.7:8082/v1", model=QWEN).id,
        "public_local": mk("f361-public-local", "local", base_url=PUBLIC, model=QWEN).id,
        "paid": mk("f361-paid", "openai_compatible", base_url="https://api.deepseek.com/v1").id,
        "doomed": mk("f361-doomed", "local", base_url=PRIVATE, model=QWEN).id,
    }
    return ids


def rule(ids):
    ref = lambda k: f"engine:{ids[k]}"  # noqa: E731
    with patch.object(config, "LOCAL_BASE_URL", PRIVATE):
        assert spend.ref_is_free("mock") and spend.ref_is_free("MOCK")
        assert spend.ref_is_free("local"), "bare local slot on a private base is free"
        assert spend.ref_is_free(ref("m5")) and spend.ref_is_free(ref("tailnet"))
        assert spend.ref_is_free("ENGINE: 0" + str(ids["m5"])), "non-canonical live refs resolve"
        assert not spend.ref_is_free(ref("public_local")), "public local endpoint is not free"
        assert not spend.ref_is_free(ref("paid")), "a paid engine is not free"
        assert not spend.ref_is_free("claude") and not spend.ref_is_free(None)
        assert not spend.ref_is_free("engine:99999"), "an unknown engine id is not free"
        # kind rule is the core of is_exempt
        assert spend.kind_is_free("local", PRIVATE) and not spend.kind_is_free("local", PUBLIC)
        assert not spend.kind_is_free("openai_compatible", PRIVATE)
        # parity with the live ledger: the same answer from is_exempt on the resolved instance
        for key in ("m5", "tailnet", "public_local", "paid"):
            prov = providers._provider_for_profile(engines.get_profile(ids[key]))
            assert spend.is_exempt(prov) == spend.ref_is_free(ref(key)), (
                key,
                spend.is_exempt(prov),
            )
        # the bare local slot on a public base is NOT free (ledger: unresolved/public priced)
    with patch.object(config, "LOCAL_BASE_URL", PUBLIC):
        assert not spend.ref_is_free("local")
        # a public local endpoint ignores the model id it reports (billable_model) -> default
        assert spend.cost_for_ref("local", QWEN, USAGE) == pricing.cost_usd(None, None, USAGE)
    # a free engine reporting a real model id is $0; a paid ref prices by model as before
    assert spend.cost_for_ref(ref("m5"), QWEN, USAGE) == 0.0
    paid = spend.cost_for_ref("claude", "claude-opus-4-8", USAGE)
    assert abs(paid - 30.0) < 1e-9, paid
    assert spend.cost_for_ref(ref("paid"), "deepseek-chat", USAGE) > 0.0
    # deleted engine: conservative (default rate), documented
    assert spend.cost_for_ref("engine:99999", QWEN, USAGE) > 0.0
    print(
        "  #361: one free-engine rule: mock/local/m5/tailnet free, public + paid + deleted priced"
    )


def _seed_job(doc_id, provider, when=None):
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            payload={"document_id": doc_id, "locale": "fr-FR"},
            status=JobStatus.COMPLETED,
            result={"provider_used": {provider: 1}, "tokens_in": M, "tokens_out": M},
            created_by="u@x.co",
            created_at=when or datetime.utcnow(),
            finished_at=when or datetime.utcnow(),
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j


def _project(name):
    with Session(engine) as s:
        p = Project(name=name)
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.docx", fmt="docx")
        s.add(d)
        s.commit()
        s.refresh(d)
        return p.id, d.id


def reports_and_budgets(ids):
    ref = lambda k: f"engine:{ids[k]}"  # noqa: E731
    with (
        patch.object(config, "LOCAL_BASE_URL", PRIVATE),
        patch.dict(config.DEFAULT_MODELS, {"local": QWEN}),
    ):
        # reports._job_cost: every free shape is 0, every other shape prices
        for prov in ("mock", "local", ref("m5"), ref("tailnet")):
            j = _seed_job(_project("f361-rj-" + prov)[1], prov)
            assert reports._job_cost(j) == 0.0, (prov, reports._job_cost(j))
        for prov in (ref("public_local"), ref("paid"), "claude", "engine:99999"):
            j = _seed_job(_project("f361-rp-" + prov)[1], prov)
            assert reports._job_cost(j) > 0.0, (prov, reports._job_cost(j))

        # #361 gate: a free engine that failed over to a paid one mid-job. The job keeps ONE
        # token total, so it must not price at $0 just because the free engine dominated.
        mixed = _seed_job(_project("f361-mixed")[1], ref("m5"))
        mixed.result = {**mixed.result, "provider_used": {ref("m5"): 9, "claude": 1}}
        want = pricing.cost_usd("claude", config.DEFAULT_MODELS["claude"], USAGE)
        assert abs(reports._job_cost(mixed) - want) < 1e-9, (reports._job_cost(mixed), want)
        mixed.result = {**mixed.result, "provider_used": {ref("m5"): 9, "mock": 1}}
        assert reports._job_cost(mixed) == 0.0, "two free engines stay free"

        # the report itself (cost, grouped by engine) shows $0 for the free engine
        pid, did = _project("f361-report")
        _seed_job(did, ref("m5"))
        _seed_job(did, "claude")
        with Session(engine) as s:
            rep = reports.run_report(
                s, reports.ReportQuery(kind="cost", group_by="none", project_id=pid), user=None
            )
        claude_only = pricing.cost_usd("claude", config.DEFAULT_MODELS["claude"], USAGE)
        assert abs(rep["rows"][0]["cost_usd"] - claude_only) < 1e-6, (rep["rows"], claude_only)

        # project budget meter + refusal check
        pid, did = _project("f361-budget-free")
        _seed_job(did, ref("m5"))
        _seed_job(did, "local")
        with Session(engine) as s:
            project_budgets.upsert_project_budget(s, pid, 0.01)
            sp = project_budgets.project_spend_this_month(s, pid)
        assert sp["ai_usd"] == 0.0 and sp["enforced_usd"] == 0.0, sp
        ok, remaining, _msg = project_budgets.project_check(pid)
        assert ok and remaining > 0.0, "a free engine must never exhaust a project budget"

        pid, did = _project("f361-budget-paid")
        _seed_job(did, ref("paid"))
        with Session(engine) as s:
            project_budgets.upsert_project_budget(s, pid, 0.01)
        ok, _r, _m = project_budgets.project_check(pid)
        assert not ok, "a paid engine still exhausts the budget"
        pid, did = _project("f361-budget-public")
        _seed_job(did, ref("public_local"))
        with Session(engine) as s:
            project_budgets.upsert_project_budget(s, pid, 0.01)
        ok, _r, _m = project_budgets.project_check(pid)
        assert not ok, "a public local endpoint still prices conservatively"

    # a deleted engine prices conservatively; a kind/URL change prices by the CURRENT kind
    pid, did = _project("f361-deleted")
    job = _seed_job(did, ref("doomed"))
    with patch.object(config, "LOCAL_BASE_URL", PRIVATE):
        assert reports._job_cost(job) == 0.0
        engines.delete_profile(ids["doomed"])
        engines._profile_memo.clear()
        assert reports._job_cost(job) > 0.0, "deleted engine: conservative default rate"
    print("  #361: reports + project meter + refusal: free engine $0, paid/public/deleted priced")


def steprun_and_ledger(ids):
    """The StepRun / Advanced actuals are the ledger's own figure: already $0 for a free
    engine. Proven through run_completion (the figure StepRun.cost_usd stores)."""

    class FreeLocal(providers.LocalProvider):
        def __init__(self):
            self._profile_base = PRIVATE

        def complete(self, prompt, system=None, task=None, payload=None, **kw):
            self.last_usage = dict(USAGE)
            return "ok"

    before = spend.spent_this_period()
    with patch.object(providers, "get_provider", return_value=FreeLocal()):
        res = providers.run_completion(
            providers.ProviderSpec(f"engine:{ids['m5']}", model=QWEN, backoff_base=0), "hi"
        )
    assert res["cost_usd"] == 0.0 and spend.spent_this_period() == before, res
    print("  #361: StepRun/run_completion actual on a free engine stays $0")


def ocr_and_image(ids):
    from app.services import image_localization, ocr

    ref = f"engine:{ids['m5']}"
    assert spend.record_ref_usage(ref, QWEN, USAGE) == 0.0
    with patch.object(config, "LOCAL_BASE_URL", PRIVATE):
        before = spend.spent_this_period()
        text, blocked = ocr._guarded_vision_call("local", QWEN, 100, lambda: ("text", dict(USAGE)))
        assert text == "text" and blocked is None
        assert spend.spent_this_period() == before, "private local OCR books nothing"

        old = image_localization.vision_call_override
        image_localization.vision_call_override = lambda png, prompt: "hello"
        try:
            with patch.dict(config.DEFAULT_MODELS, {"local": QWEN}):
                image_localization._guarded_vision_call(b"\x89PNG" * 600_000, "p", "local")
        finally:
            image_localization.vision_call_override = old
        assert spend.spent_this_period() == before, "private local image call books nothing"
    with patch.object(config, "LOCAL_BASE_URL", PUBLIC):
        before = spend.spent_this_period()
        ocr._guarded_vision_call("local", QWEN, 100, lambda: ("text", dict(USAGE)))
        assert spend.spent_this_period() > before, "a public local endpoint books conservatively"
    before = spend.spent_this_period()
    ocr._guarded_vision_call("claude", "claude-opus-4-8", 100, lambda: ("t", dict(USAGE)))
    assert abs(spend.spent_this_period() - before - 30.0) < 1e-6, "cloud OCR books as before"
    print("  #361: OCR + image localization: private local books $0; public and cloud book")


def default_gemini_model():
    assert config.DEFAULT_MODELS["gemini"] == "gemini-3.6-flash", config.DEFAULT_MODELS
    # the default id matches its own pricing row (not the bare-gemini fallback)
    assert pricing.rates_for("gemini-3.6-flash", prompt_tokens=1) != pricing.rates_for("gemini")
    assert "gemini-2.0-flash" not in config.DEFAULT_MODELS.values()
    print("  #361: default Gemini model is gemini-3.6-flash and prices on its own row")


def run():
    ids = _engines()
    rule(ids)
    reports_and_budgets(ids)
    steprun_and_ledger(ids)
    ocr_and_image(ids)
    default_gemini_model()
