"""Hermetic showcase seed and 56 x 2 classic execution proof.

The 8 Studio (Advanced mission) runs of the original suite are not part of the open-core tree.
"""

import io
import os
import sys
import time
from contextlib import redirect_stdout
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_showcase_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    ApiKey,
    Customer,
    Document,
    Glossary,
    GlossaryEntry,
    GlossaryPair,
    MemoryEvent,
    Project,
    RunStatus,
    Segment,
    StepRun,
    Vendor,
    VendorDispatch,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    apikeys,
    content,
    customers,
    glossary_module,
    orchestrator,
    security,
    settings,
    workflows,
)
from app.services.providers import Provider, register_provider
from scripts import showcase_seed as seed
from tests._piece_patch import patch_pieces


def invoke(args, expected=0, echo=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = seed.main(args)
    output = buf.getvalue()
    # Never include key-bearing output in assertion diagnostics or evidence.
    assert code == expected, (args, code, output if "--mint-key" not in args else "withheld")
    if echo:
        assert "--mint-key" not in args
        print(output, end="")
    return output


def customer(s):
    return s.exec(select(Customer).where(Customer.name == seed.CUSTOMER_NAME)).one()


def counts(s):
    return tuple(
        len(s.exec(select(m)).all())
        for m in (
            Customer,
            Glossary,
            GlossaryEntry,
            GlossaryPair,
            Workflow,
            WorkflowStep,
            ApiKey,
        )
    )


def document(s, pid, target):
    doc = Document(
        project_id=pid, filename="showcase.txt", fmt="txt", mode="segmented", status="parsed"
    )
    s.add(doc)
    s.commit()
    s.refresh(doc)
    for i, source in enumerate(
        ("Welcome to Acme Cloud.", "Hello {placeholder}.", "Have a nice day.")
    ):
        s.add(
            Segment(
                document_id=doc.id,
                order_index=i,
                source_text=source,
                target_text="",
                target_locale=target,
            )
        )
    s.commit()
    return doc.id


def targets(s, did):
    s.expire_all()
    rows = s.exec(
        select(Segment).where(Segment.document_id == did).order_by(Segment.order_index)
    ).all()
    assert len(rows) == 3 and all(r.target_text.strip() for r in rows)
    assert "{placeholder}" in rows[1].target_text


def seed_shape():
    validated = []
    real_validate = settings.validate_value

    def validate(key, value):
        validated.append(key)
        return real_validate(key, value)

    with patch_pieces("app.services.settings", "validate_value", validate):
        invoke(["--dry-run", "--settings", "--allowed-hosts", "demo.example.com"], echo=True)
    assert set(validated) == set(seed.SETTINGS_KEYS), "M4: fresh dry-run bypassed public validation"
    with Session(engine) as s:
        assert not any(counts(s)), "fresh dry-run wrote seed rows"
    init_db()
    with Session(engine) as s:
        unmarked = content.customer_create(s, seed.CUSTOMER_NAME)
        unmarked_id = unmarked.id
        before = counts(s)
    invoke([], 2)
    invoke(["--cleanup"], 2)
    with Session(engine) as s:
        assert counts(s) == before
        customers.delete_customer(s, unmarked_id)
    original_settings = {key: settings.get(key) for key in seed.SETTINGS_KEYS}
    with Session(engine) as s:
        before = counts(s)
    invoke(["--dry-run", "--settings"], echo=True)
    with Session(engine) as s:
        assert counts(s) == before
    assert {key: settings.get(key) for key in seed.SETTINGS_KEYS} == original_settings
    invoke([], echo=True)
    assert {key: settings.get(key) for key in seed.SETTINGS_KEYS} == original_settings
    with Session(engine) as s:
        c = customer(s)
        manifest = c.settings["showcase_seed"]
        assert manifest["marker"] == seed.SHOWCASE_MARKER and manifest["version"] == 1
        g = s.get(Glossary, manifest["glossary_id"])
        assert g.status == "approved" and g.content_type_id is None
        entries = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == g.id)).all()
        assert len(entries) == 40
        assert len({e.source_term for e in entries}) == 8
        assert sum(e.dnt for e in entries) >= 15
        pairs = s.exec(select(GlossaryPair).where(GlossaryPair.glossary_id == g.id)).all()
        assert len(pairs) == 5
        for term in {e.source_term for e in entries}:
            assert {e.target_locale for e in entries if e.source_term == term} == set(
                seed.GLOSSARY_LOCALES
            )
        assert len(manifest["workflow_ids"]) == 2
        for idx, wid in enumerate(manifest["workflow_ids"]):
            wf = s.get(Workflow, wid)
            assert wf.name == seed.TIERS[idx][1]
            assert wf.scope == "global" and wf.customer_id == c.id
            assert wf.source_locale is None and wf.target_locale is None
            assert wf.created_via == seed.SHOWCASE_MARKER
            steps = s.exec(
                select(WorkflowStep)
                .where(WorkflowStep.workflow_id == wid)
                .order_by(WorkflowStep.order_index)
            ).all()
            assert [st.step_type for st in steps] == (
                ["llm_agent"] if idx == 0 else ["llm_agent", "lqa"]
            )
            assert steps[0].name == "MT" and not steps[0].provider and not steps[0].model
            if idx:
                assert steps[0].glossary_id == g.id
                assert steps[1].name == "Gate" and steps[1].lqa_threshold == 90
        customers.update_customer_settings(s, c.id, {"operator_note": "keep me"})
        before = counts(s)
    invoke([])
    with Session(engine) as s:
        assert counts(s) == before
        assert customer(s).settings["operator_note"] == "keep me"
    invoke(["--mint-key", "missing@example.invalid"], 2)
    invoke(["--settings"], echo=True)
    print("SEED SHAPE / DRY RUN / IDEMPOTENCE OK")


def settings_reporting():
    before = {key: settings.get(key) for key in seed.SETTINGS_KEYS}
    with patch.object(seed, "_seed", side_effect=RuntimeError("fixture seed failure")):
        output = invoke(["--settings"], 1)
    assert "setting | before | after" not in output, "L2: failed seed printed an after table"
    assert {key: settings.get(key) for key in seed.SETTINGS_KEYS} == before
    emitted = []
    real_emit = seed._emit

    def emit(text):
        if text == "setting | before | after":
            assert settings.get("spend_cap_usd") == "26", "L2: table preceded settings write"
            emitted.append(text)
        real_emit(text)

    with patch.object(seed, "_emit", emit):
        invoke(["--settings", "--spend-cap", "26"])
    assert emitted, "L2: successful settings write omitted table"
    settings.set_many(before)
    settings.apply_to_config()
    print("L2 SETTINGS TABLE AFTER WRITE / OMITTED ON SEED FAILURE OK")


def repairs_and_refusals():
    with Session(engine) as s:
        c = customer(s)
        manifest = c.settings["showcase_seed"]
        gid = manifest["glossary_id"]
        quick, reviewed = manifest["workflow_ids"]
        entries = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        glossary_module.remove_term(s, gid, entries[0].id)
        mt = s.exec(
            select(WorkflowStep).where(
                WorkflowStep.workflow_id == reviewed, WorkflowStep.name == "MT"
            )
        ).one()
        workflows.delete_step(s, mt.id, workflow_id=reviewed)
    invoke([])
    with Session(engine) as s:
        assert (
            len(s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()) == 40
        )
        steps = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == reviewed)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert [st.name for st in steps] == ["MT", "Gate"]
        assert customer(s).settings["showcase_seed"]["workflow_ids"] == [quick, reviewed]
        workflows.delete_workflow(s, quick)
        glossary_module.delete(s, gid)
    invoke([])
    with Session(engine) as s:
        manifest = customer(s).settings["showcase_seed"]
        assert all(s.get(Workflow, wid) for wid in manifest["workflow_ids"])
        assert manifest["workflow_ids"][1] == reviewed
        mt = s.exec(
            select(WorkflowStep).where(
                WorkflowStep.workflow_id == reviewed, WorkflowStep.name == "MT"
            )
        ).one()
        assert mt.glossary_id == manifest["glossary_id"]
    before = {key: settings.get(key) for key in seed.SETTINGS_KEYS}
    for cap in ("0", "-1", "nan", "inf"):
        invoke(["--settings", "--spend-cap", cap], 2)
    invoke(["--settings", "--retention-days", "0"], 2)
    settings.validate_value("app_base_url", "https://demo.example.com")
    settings.set_many({"app_base_url": "https://demo.example.com"})
    refusal = invoke(["--settings", "--allowed-hosts", "other.example.com"], 2)
    assert "demo.example.com" in refusal
    assert {key: settings.get(key) for key in seed.SETTINGS_KEYS} == before
    invoke(["--settings", "--allowed-hosts", "demo.example.com,testserver"], echo=True)
    assert "demo.example.com" in settings.get("security_allowed_hosts")
    # Restore the test ingress values for TestClient, through the settings seam.
    settings.set_many({"app_base_url": "", "security_allowed_hosts": ""})
    settings.apply_to_config()
    print("MISSING PARENTS / TERMS / STEPS / SETTINGS REFUSALS OK")


def key_and_clean_install():
    user = security.create_user(
        "showcase@example.invalid",
        "Showcase test",
        "admin",
        "Showcase-fixture-2026!",  # pregate: allow-secret offline fixture
        enforce_policy=False,
    )
    # A pre-fix manifest key must be replaced, not reused without docs access.
    legacy, _ = apikeys.create_key(user.id, name=seed.KEY_NAME, scopes="api")
    with Session(engine) as s:
        c = customer(s)
        manifest = dict(c.settings["showcase_seed"])
        manifest["api_key_ids"] = [legacy.id]  # pregate: allow-secret manifest holds key ids only
        customers.update_customer_settings(s, c.id, {"showcase_seed": manifest})
    output = invoke(["--mint-key", user.email])
    prefix = "lsu_"  # pregate: allow-secret key prefix used to find the one mint line
    token = next(word for word in output.split() if word.startswith(prefix))
    assert output.count(token) == 1
    key = apikeys.verify_key(token)
    assert key and key.scopes == "api docs", "K1: BFF key lacks api docs grants"
    with Session(engine) as s:
        assert not s.get(ApiKey, legacy.id).active, "legacy api-only key stayed active"
    with Session(engine) as s:
        before = counts(s)
    repeat = invoke(["--mint-key", user.email])
    assert "lsu_" not in repeat and "reused" in repeat
    invoke(["--dry-run", "--mint-key", user.email])
    with Session(engine) as s:
        assert counts(s) == before
    from fastapi.testclient import TestClient

    from app.main import app

    # No lifespan: the scheduler is unrelated to token authentication and pair execution.
    client = TestClient(app)
    assert client.get("/api/projects").status_code == 401
    response = client.get("/api/projects", headers={"X-Loc-Token": token})
    assert response.status_code == 200, (response.status_code, response.json())
    print("API GET /api/projects 200", response.json())
    with Session(engine) as s:
        p = content.project_create(s, "API document fixture", target_locales="es-ES")
        did = document(s, p.id, "es-ES")
    response = client.get(f"/api/documents/{did}/segments", headers={"X-Loc-Token": token})
    assert response.status_code == 200, (response.status_code, response.json())
    print("API GET /api/documents/{id}/segments 200", response.json())
    with Session(engine) as s:
        c = customer(s)
        assert key.id in c.settings["showcase_seed"]["api_key_ids"]
        other_c = content.customer_create(s, "Unrelated customer")
        other_g = glossary_module.create(s, seed.GLOSSARY_NAME)
        other_w = workflows.create_workflow(s, seed.TIERS[0][1])
        survivors = (other_c.id, other_g.id, other_w.id)
        cid = c.id
    preview = invoke(["--cleanup", "--dry-run"], echo=True)
    assert "would delete customer" in preview and "would refuse" not in preview
    assert "would revoke key" in preview and "would delete workflow" in preview
    invoke(["--cleanup"], echo=True)
    with Session(engine) as s:
        assert s.get(Customer, cid) is None
        assert not s.get(ApiKey, key.id).active
        for model, ident in zip((Customer, Glossary, Workflow), survivors, strict=True):
            assert s.get(model, ident) is not None
    assert apikeys.verify_key(token) is None
    assert client.get("/api/projects", headers={"X-Loc-Token": token}).status_code == 401
    client.close()
    print("API AUTH / CLEAN INSTALL CLEANUP / UNRELATED SURVIVORS OK")


def one_visitor_cleanup():
    invoke(["--mint-key", "showcase@example.invalid"])
    with Session(engine) as s:
        c = customer(s)
        manifest = c.settings["showcase_seed"]
        cid = c.id
        project = content.project_create(
            s, "One visitor", source_locale="en-US", target_locales="fr-FR", customer_id=str(cid)
        )
        pid = project.id
        assert customers.reference_count(s, cid)["projects"] == 1
        before = counts(s)
    output = invoke(["--cleanup", "--dry-run"], echo=True)
    assert "would refuse (still referenced by 1 projects)" in output, (
        "M3: dry-run hid customer refusal"
    )
    assert "would delete customer" not in output
    with Session(engine) as s:
        assert counts(s) == before
        assert all(s.get(ApiKey, kid).active for kid in manifest["api_key_ids"])
    output = invoke(["--cleanup"], 2, echo=True)
    assert "1 projects" in output and "revoked key" in output
    with Session(engine) as s:
        assert s.get(Customer, cid) and s.get(Project, pid)
        assert s.get(Glossary, manifest["glossary_id"]) is None
        assert all(s.get(Workflow, wid) is None for wid in manifest["workflow_ids"])
        assert all(not s.get(ApiKey, kid).active for kid in manifest["api_key_ids"])
    print("ONE VISITOR: EXIT 2 / KEY REVOKED / PARENTS REMOVED / CUSTOMER + PROJECT INTACT OK")


def pairs():
    invoke([])
    start = time.monotonic()
    classic_count = 0
    with Session(engine) as s:
        c = customer(s)
        wids = c.settings["showcase_seed"]["workflow_ids"]
        for source in seed.ADVERTISED_LOCALES:
            dests = [t for t in seed.ADVERTISED_LOCALES if t != source]
            p = content.project_create(
                s,
                "Pairs " + source,
                source_locale=source,
                target_locales=",".join(dests),
                customer_id=str(c.id),
            )
            for target in dests:
                for idx, wid in enumerate(wids):
                    did = document(s, p.id, target)
                    observed = []
                    real_match = glossary_module.match_terms_merged

                    def observe(*args, _real=real_match, _observed=observed, **kwargs):
                        result = _real(*args, **kwargs)
                        _observed.append(result)
                        return result

                    tick = time.monotonic()
                    with patch_pieces(
                        "app.services.glossary_module", "match_terms_merged", observe
                    ):
                        summary = orchestrator.run_workflow(
                            s, wid, did, target, created_by="showcase-test"
                        )
                    assert summary["status"] == "completed", summary
                    run = s.get(WorkflowRun, summary["run_id"])
                    assert run.status == "completed" and run.finished_at is not None
                    records = s.exec(select(StepRun).where(StepRun.workflow_run_id == run.id)).all()
                    assert len(records) == idx + 1
                    assert all(r.status == "completed" for r in records), records
                    assert all(not r.error for r in records if r.gate is None), records
                    if idx:
                        assert records[-1].gate == "passed"
                    targets(s, did)
                    assert observed, "real glossary matcher was not observed"
                    bound = source == "en-US" and target in seed.GLOSSARY_LOCALES
                    if bound and idx:
                        assert any(observed), "bound pair lost all glossary hits"
                    if not bound:
                        assert not any(observed), observed
                    classic_count += 1
                    print(
                        f"PAIR {source} -> {target} {seed.TIERS[idx][0]} PASS "
                        f"{time.monotonic() - tick:.3f}s"
                    )
    n = len(seed.ADVERTISED_LOCALES)
    assert classic_count == n * (n - 1) * 2, (classic_count, n)
    print(f"MATRIX {classic_count} classic PASS {time.monotonic() - start:.3f}s")


class ManglingProvider(Provider):
    name = "showcase_mangle"

    def __init__(self):
        self.calls = []

    def complete(self, prompt, system=None, task=None, payload=None, **kwargs):
        payload = payload or {}
        result = " ".join("<" + word + ">" for word in payload.get("text", "").split())
        self.calls.append((payload, result))
        return result


def dnt_adversary():
    invoke(["--settings"])
    provider = ManglingProvider()
    register_provider(provider.name, provider)
    with Session(engine) as s:
        c = customer(s)
        wid = c.settings["showcase_seed"]["workflow_ids"][1]
        mt = s.exec(
            select(WorkflowStep).where(WorkflowStep.workflow_id == wid, WorkflowStep.name == "MT")
        ).one()
        original_provider = mt.provider
        workflows.update_step(s, mt.id, provider=provider.name)
        try:
            for target in seed.GLOSSARY_LOCALES:
                p = content.project_create(
                    s,
                    "DNT adversary " + target,
                    source_locale="en-US",
                    target_locales=target,
                    customer_id=str(c.id),
                )
                did = document(s, p.id, target)
                observed = []
                real_match = glossary_module.match_terms_merged

                def observe(*args, _real=real_match, _observed=observed, **kwargs):
                    result = _real(*args, **kwargs)
                    _observed.extend(result)
                    return result

                provider.calls.clear()
                with patch_pieces("app.services.glossary_module", "match_terms_merged", observe):
                    summary = orchestrator.run_workflow(
                        s, wid, did, target, created_by="showcase-test"
                    )
                assert provider.calls, "H2: mangling provider was not called"
                assert any("<Acme>" in result for _, result in provider.calls), provider.calls
                s.expire_all()
                row = s.exec(
                    select(Segment).where(Segment.document_id == did).order_by(Segment.order_index)
                ).first()
                print(
                    f"DNT ADVERSARY {target}: status={summary['status']} target={row.target_text!r}"
                )
                assert any(
                    t.get("dnt") and t.get("source_term") == "Acme Cloud" for t in observed
                ), "H2: glossary DNT entries were not offered to the step"
                offered = [t for payload, _ in provider.calls for t in payload.get("glossary", [])]
                assert any(
                    t.get("dnt") and t.get("source_term") == "Acme Cloud" for t in offered
                ), "H2: provider did not receive DNT entries"
                print(
                    f"DNT OFFERED {target}: matcher and provider payload confirmed; "
                    "preservation NOT claimed"
                )
        finally:
            workflows.update_step(s, mt.id, provider=original_provider)
    print("DNT ADVERSARY FIVE BOUND REVIEWED PAIRS OK")


def cleanup_ownership():
    invoke([])
    user = security.create_user(
        "foreign@example.invalid", "Foreign", "admin", "Foreign-fixture-2026!", enforce_policy=False
    )  # pregate: allow-secret offline fixture
    key, _ = apikeys.create_key(user.id, name=seed.KEY_NAME)
    with Session(engine) as s:
        c = content.customer_create(s, "Foreign ring")
        wf = workflows.create_workflow(
            s, "Foreign marked", customer_id=str(c.id), created_via=seed.SHOWCASE_MARKER
        )
        wid = wf.id
    invoke(["--cleanup"])
    with Session(engine) as s:
        assert s.get(ApiKey, key.id).active, "H1: cleanup revoked unrelated same-named key"
        assert s.get(Workflow, wid), "H1: cleanup deleted another customer's marked workflow"
    print("H1 FOREIGN SAME-NAMED KEY AND MARKED WORKFLOW SURVIVE OK")


def run_history_cleanup():
    """#137: --cleanup meets a seeded workflow that has RUN HISTORY.

    Two shapes, both of which used to escape workflows.delete_workflow as a raw
    sqlite3.IntegrityError and surface as the seed's generic exit-1 'unexpected error':
      * a MemoryEvent captured against one of the workflow's runs -- un-scopable, so the
        cleanup must simply SUCCEED (exit 0) and the event must outlive the run;
      * a VendorDispatch leg anchored to one of its runs -- a durable commercial record, so
        the cleanup must REFUSE cleanly (exit 2, the reason printed, no traceback), and the
        --dry-run forecast must say so ahead of time without deleting anything.
    """
    invoke([])
    with Session(engine) as s:
        c = customer(s)
        cid = c.id
        wids = list(c.settings["showcase_seed"]["workflow_ids"])
        assert len(wids) >= 2, wids
        # a project with NO customer: the run history under test must be the only thing
        # standing between the cleanup and a clean customer delete.
        p = Project(name="Run history", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        did = document(s, p.id, "es-ES")
        runs = []
        for wid in wids[:2]:
            r = WorkflowRun(workflow_id=wid, document_id=did, status=RunStatus.COMPLETED)
            s.add(r)
            s.commit()
            s.refresh(r)
            runs.append(r.id)
        ev = MemoryEvent(
            kind="edit",
            document_id=did,
            workflow_run_id=runs[0],
            before_text="before",
            after_text="after",
        )
        s.add(ev)
        vendor = Vendor(name="Showcase vendor")
        s.add(vendor)
        s.commit()
        s.refresh(ev)
        s.refresh(vendor)
        ev_id = ev.id
        leg = VendorDispatch(vendor_id=vendor.id, workflow_run_id=runs[1], target_locale="es-ES")
        s.add(leg)
        s.commit()
        s.refresh(leg)
        leg_id = leg.id
        gid = c.settings["showcase_seed"]["glossary_id"]
    # (1) the dry-run FORECASTS the workflow refusal and writes nothing.
    output = invoke(["--cleanup", "--dry-run"], echo=True)
    assert "[dry-run] would refuse" in output, "#137: dry-run hid the workflow refusal"
    assert f"workflow id={wids[1]}" in output, output
    assert "vendor dispatch" in output, output
    # LOW-2 (a): the dry-run STOPS at the first refusing workflow, exactly as the live path
    # returns there -- it must NOT go on to forecast the glossary or customer removal (the
    # old forecast continued past the refusal and subtracted every workflow from the customer
    # projection, an outcome the live run never produces).
    assert "would delete glossary" not in output, "LOW-2: dry-run forecast past the refusal"
    assert "customer id=" not in output, "LOW-2: dry-run forecast the customer past the refusal"
    assert f"would stop here (workflow id={wids[1]}" in output, output
    with Session(engine) as s:
        assert all(s.get(Workflow, wid) is not None for wid in wids)
        assert s.get(VendorDispatch, leg_id) is not None
    # (2) the live cleanup refuses CLEANLY: exit 2, the reason, no generic handler.
    output = invoke(["--cleanup"], 2, echo=True)
    assert "unexpected error" not in output, "#137: the generic handler masked the refusal"
    assert "Traceback" not in output, output
    assert "vendor dispatch" in output and "archive" in output.lower(), output
    assert "keys are already revoked" in output.lower(), "#137: the key state was not stated"
    with Session(engine) as s:
        # the memory-event workflow went cleanly; its event OUTLIVED the run un-scoped
        assert s.get(Workflow, wids[0]) is None, "#137: the un-scopable run history blocked"
        surviving = s.get(MemoryEvent, ev_id)
        assert surviving is not None and surviving.workflow_run_id is None
        # the vendor-leg workflow, its run and the leg all survive the refusal
        assert s.get(Workflow, wids[1]) is not None
        assert s.get(WorkflowRun, runs[1]) is not None
        assert s.get(VendorDispatch, leg_id) is not None
        assert s.get(Customer, cid) is not None and s.get(Glossary, gid) is not None
    # (3) clear the blocker (the operator's alternative to archiving) -> a clean exit 0.
    with Session(engine) as s:
        s.delete(s.get(VendorDispatch, leg_id))
        s.commit()
    invoke(["--cleanup"], echo=True)
    with Session(engine) as s:
        assert s.get(Customer, cid) is None
        assert all(s.get(Workflow, wid) is None for wid in wids)
        assert s.get(Glossary, gid) is None
        assert s.get(MemoryEvent, ev_id) is not None  # still un-scoped, still there
    print("#137 RUN-HISTORY CLEANUP: EVENT UN-SCOPED / VENDOR LEG EXIT 2 / DRY-RUN FORECAST OK")


def matrix_counts():
    invoke(["--settings"])
    output = io.StringIO()
    with patch.object(seed, "ADVERTISED_LOCALES", ("en-US", "es-ES")), redirect_stdout(output):
        pairs()
    summary = next(line for line in output.getvalue().splitlines() if line.startswith("MATRIX"))
    print(summary)
    assert "MATRIX 4 classic PASS" in summary, "M1: reduced matrix overstated coverage"
    print("M1 REDUCED MATRIX COUNTS OK")


def retained_cleanup():
    with Session(engine) as s:
        c = customer(s)
        cid = c.id
        manifest = c.settings["showcase_seed"]
        before = [p.id for p in s.exec(select(Project).where(Project.customer_id == cid)).all()]
    output = invoke(["--cleanup"], 2, echo=True)
    assert "the customer stays until retention purge" in output
    with Session(engine) as s:
        assert s.get(Customer, cid) is not None
        assert s.get(Glossary, manifest["glossary_id"]) is None
        assert all(s.get(Workflow, wid) is None for wid in manifest["workflow_ids"])
        assert [
            p.id for p in s.exec(select(Project).where(Project.customer_id == cid)).all()
        ] == before
    print("RETAINED VISITOR CLEANUP OK")


if __name__ == "__main__":
    seed_shape()
    settings_reporting()
    repairs_and_refusals()
    key_and_clean_install()
    cleanup_ownership()
    run_history_cleanup()
    one_visitor_cleanup()
    matrix_counts()
    pairs()
    retained_cleanup()
    # Hostile MT leaves history that the existing workflow delete cannot cascade.
    # Its separate evidence runs after the ordinary mock lifecycle cleanup proof.
    dnt_adversary()
    print("SHOWCASE PAIRS OK")
