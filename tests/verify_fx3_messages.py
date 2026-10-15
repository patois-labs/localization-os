"""FX-3 operator messages, offline through the maintained suite wrapper."""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_fx3_")
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli import _remote_impl_part4 as remote_workflows
from app.cli.__main__ import main as cli
from app.db import engine, init_db
from app.main import app
from app.models import Document, Project, StepRun, TargetLocale, WorkflowRun, WorkflowStep
from app.services import (
    apikeys,
    engines,
    jev_inspector,
    package_view,
    pair_providers,
    qe,
    qe_autofix,
    qe_jev,
    qe_jev_loop,
    run_overlay,
    security,
    workflows,
)


def main():
    init_db()
    errors = []

    def check(ok, message):
        if not ok:
            errors.append(message)
            print("FAIL " + message)

    with Session(engine) as session:
        wf = workflows.create_workflow(session, "FX3")
        step = workflows.add_step(session, wf.id, provider="mock")
        for ref in ("jevv", "engine:abc", "engine:999999", "deepl"):
            for edit in (False, True):
                try:
                    if edit:
                        workflows.update_step(session, step.id, qe_provider=ref)
                    else:
                        workflows.add_step(session, wf.id, provider="mock", qe_provider=ref)
                except ValueError as exc:
                    check("judge" in str(exc).lower(), "#189 names judge")
                else:
                    check(False, f"#189 accepted {ref}, edit={edit}")

    judge = engines.create_profile("FX3 judge", "jev_local", base_url="http://127.0.0.1:8077")
    target = qe_jev.resolve_target(f"engine:{judge.id}")
    with Session(engine) as session:
        project = Project(name="FX3 pairs", source_locale="en")
        session.add(project)
        session.flush()
        session.add(TargetLocale(project_id=project.id, locale="de"))
        session.commit()
        for ref in ("jev", f"engine:{judge.id}"):
            try:
                pair_providers.validate_mapping(session, project, {("en", "de"): ref})
            except pair_providers.PairProviderError as exc:
                check(
                    "QE judge engine" in str(exc) and "scope" not in str(exc), "#209 judge wording"
                )
            else:
                check(False, "#209 judge accepted")
    user = security.create_user("fx3@test.invalid", "FX3", "admin", "fx3-test-password")
    _, token = apikeys.create_key(user.id, "fx3", scopes="api docs admin")
    headers = {"X-Loc-Token": token}
    client = TestClient(app, headers=headers)
    for ref in ("jevv", "deepl", "engine:abc", "engine:999999"):
        for gate in (False, True):
            with Session(engine) as session:
                legacy = workflows.add_step(session, wf.id, provider="mock")
                legacy.qe_provider = ref
                legacy.qe_gate = gate
                session.add(legacy)
                session.commit()
                sid = legacy.id
            form = {"name": "Renamed legacy", "provider": "mock", "qe_provider": ref}
            if gate:
                form["qe_gate"] = "1"
            response = client.post(
                f"/workflows/{wf.id}/steps/{sid}/edit", data=form, follow_redirects=False
            )
            check(response.status_code == 303, f"M1 web rename {ref}, gate={gate}")
            with Session(engine) as session:
                saved = session.get(WorkflowStep, sid)
                check(saved.name == "Renamed legacy" and saved.qe_provider == ref, "M1 persisted")
            response = client.post(
                f"/workflows/{wf.id}/steps/{sid}/edit",
                data={**form, "name": "Refused rename", "qe_provider": "new-bad-judge"},
                follow_redirects=False,
            )
            check(response.status_code == 400, "M1 changed bad judge refused")
            with Session(engine) as session:
                saved = session.get(WorkflowStep, sid)
                check(
                    saved.qe_provider == ref and saved.name != "Refused rename", "M1 atomic refusal"
                )
    engines.update_profile(judge.id, enabled=False)
    response = client.post(
        f"/api/workflows/{wf.id}/steps",
        json={"provider": "mock", "qe_provider": f"engine:{judge.id}", "qe_gate": True},
    )
    check(
        response.status_code == 200 and "disabled" in str(response.json().get("warnings")),
        "#189 disabled saves with warning",
    )
    engines.update_profile(judge.id, enabled=True)
    disabled = engines.create_profile(
        "FX3 disabled translator", "openai_compatible", base_url="http://127.0.0.1:8079"
    )
    engines.update_profile(disabled.id, enabled=False)
    for gate, judge_ref, expected in (
        (False, None, False),
        (False, f"engine:{disabled.id}", False),
        (True, "mock", False),
        (True, None, True),
    ):
        response = client.post(
            f"/api/workflows/{wf.id}/steps",
            json={"provider": f"engine:{disabled.id}", "qe_gate": gate, "qe_provider": judge_ref},
        )
        check(response.status_code == 200, "L1 disabled translator saves")
        warning = str(response.json().get("warnings"))
        check(("QE judge" in warning) == expected, f"L1 correct disabled role: {gate}, {judge_ref}")
    prompt = qe_jev_loop.build_prompt(
        "Hello",
        "en",
        "de",
        [
            {
                "round": 1,
                "translation": "Hallo",
                "jev": {"score": 50, "skipped": False},
                "llm": {"score": 80, "rationale": "Fluent"},
            }
        ],
        70,
    )
    check("Post-editor LLM's own second opinion: 80; Fluent" in prompt, "L2 #214 prompt")
    help_text = " ".join(client.get("/help").text.split())
    check(
        "also asks the post-editor LLM for its own score" in help_text
        and "shown to it as a second opinion" in help_text
        and "adds the post-editor LLM's own second opinion on accepted rewrites" in help_text,
        "L2 #214 help",
    )
    for raw, secret in (
        ("Authorization: Bearer old-token-123456789", "old-token-123456789"),
        ("x-api-key: old-api-key-123456789", "old-api-key-123456789"),
        ("https://old-user:old-password@example.invalid/fail", "old-password"),
    ):
        clean = qe_jev.engine_error(raw)
        check(secret not in clean and "[redacted]" in clean, "L3 pattern " + raw.split(":")[0])
    query_names = (
        "key",
        "api_key",
        "token",
        "access_token",
        "API-KEY",
        "ApiKey",
        "refresh_token",
        "client_secret",
        "password",
        "X-Goog-Api-Key",
    )
    historical_error = "model load failed: https://example.invalid/fail?mode=debug&" + "&".join(
        f"{name}=rotated-secret-{index}" for index, name in enumerate(query_names)
    )
    for name in query_names:
        for prefix in ("?", "&"):
            clean = qe_jev.engine_error(
                f"failed https://example.invalid/{prefix}{name}=old%2Bsecret&ok=1"
            )
            check("old%2Bsecret" not in clean and "ok=1" in clean, "L3 query " + prefix + name)
            check(qe_jev.engine_error(clean) == clean, "L3 query redaction idempotent")
    for ref in ("jevv", "engine:abc", "engine:999999", "deepl"):
        response = client.post(
            f"/api/workflows/{wf.id}/steps", json={"provider": "mock", "qe_provider": ref}
        )
        check(response.status_code == 400, "#189 API refuses " + ref)
    for options, words in (
        ({"qe_autofix": True}, "gate is off"),
        ({"qe_autofix": True, "qe_gate": True, "qe_provider": "mock"}, "not a Jev judge"),
    ):
        response = client.post(
            f"/api/workflows/{wf.id}/steps", json={"provider": "mock", **options}
        )
        check(
            response.status_code == 200 and words in str(response.json().get("warnings")),
            "#193 API warning " + response.text,
        )
        response = client.post(f"/api/workflows/{wf.id}/steps/{step.id}/edit", json=options)
        check(
            response.status_code == 200 and words in str(response.json().get("warnings")),
            "#193 edit warning " + response.text,
        )
        output = io.StringIO()
        argv = ["--json", "workflow", "add-step", str(wf.id), "--provider", "mock", "--qe-autofix"]
        if options.get("qe_gate"):
            argv += ["--qe-gate", "--qe-provider", "mock"]
        with contextlib.redirect_stdout(output):
            code = cli(argv)
        check(
            code == 0 and words in str(json.loads(output.getvalue()).get("warnings")),
            "#193 CLI warning",
        )
    body = client.get(f"/api/workflows/{wf.id}").json()
    rendered = []
    emitter = SimpleNamespace(
        as_json=False,
        say=lambda text="": rendered.append(text),
        table=lambda rows, **kw: rendered.append(str(rows)),
    )
    with patch.object(
        remote_workflows, "_client", return_value=SimpleNamespace(get=lambda path: body)
    ):
        remote_workflows.workflow_show(SimpleNamespace(workflow_id=wf.id), emitter)
    check("+fix" not in str(rendered) and "cannot run" in str(rendered), "#193 remote show honest")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli(["workflow", "show", str(wf.id)])
    check(
        code == 0 and "+fix" not in output.getvalue() and "cannot run" in output.getvalue(),
        "#193 local show honest",
    )
    web = client.post(
        f"/workflows/{wf.id}/steps",
        data={"provider": "mock", "qe_autofix": "1"},
        follow_redirects=False,
    )
    from urllib.parse import unquote

    check(
        web.status_code == 303 and "gate is off" in unquote(web.headers.get("location", "")),
        "#193 web warning",
    )
    record = qe_autofix.record(outcome="skipped", skipped_reason="unsure", unsure=["terminology"])
    check("terminology was unsure" in record["message"], "#201 dimension named")
    flag = qe.qe_flag(None, "", 70, False, True, reason="not_configured")
    check("QE skipped: judge not configured" in flag["message"], "#189 skip named")
    check(
        qe_autofix.display_status({"qe_gate": True, "qe_autofix": True, "qe_provider": "jev"})
        == "auto-fix on",
        "#193 real auto-fix shown",
    )
    view = package_view.step_facts(
        {"step_type": "llm_agent", "qe_gate": True, "qe_provider": "jev", "qe_autofix": True}
    )
    fact = next(f for f in view["verify"] if f["key"] == "qe_gate")
    check(
        "judge jev" in fact["value"]
        and "auto-fix on" in fact["value"]
        and "70/100" in fact["value"]
        and "%" not in fact["value"],
        "#193 VERIFY details",
    )
    failure = run_overlay.failure_shape(
        [
            SimpleNamespace(
                id=1, status="failed", error="failed to load model: requires about 45 GB", step_id=1
            )
        ],
        [],
    )
    check("requires about 45 GB" in str(failure), "#215 engine message exposed")
    for exc, words in (
        (httpx.ReadTimeout("Bearer secret"), "timed out after"),
        (httpx.ConnectError("Bearer secret"), "server not reachable"),
        (httpx.ProxyError("Bearer secret"), "server not reachable"),
    ):
        with patch.object(qe_jev, "_post", side_effect=exc), patch.object(qe_jev.time, "sleep"):
            card = qe_jev.score_segment("Hello", "Hallo", "en", "de", judge=target)
            tried = jev_inspector.try_it(judge, "Hello", "Hallo", "en", "de", 70)
        check(words in (tried.get("message") or ""), "#207 Try it guidance")
        if isinstance(exc, httpx.ReadTimeout):
            check(tried.get("timed_out"), "#207 Try it timeout marked")

        def offline(req, exc=exc):
            raise exc

        ok, message = engines.test_profile(judge.id, transport=httpx.MockTransport(offline))
        check(not ok and words in message and "secret" not in message, "#207 Test probe guidance")
        check(words in (card.get("error") or ""), "#207 " + words)
        check("secret" not in str(card), "#207 secret-free")
        if isinstance(exc, httpx.ReadTimeout):
            check(card.get("timed_out"), "#207 per-read timeout marked")
    for status in (401, 403):
        with patch.object(qe_jev, "_post", return_value=(status, {})):
            card = qe_jev.score_segment("Hello", "Hallo", "en", "de", judge=target)
        check("server rejected the key" in (card.get("error") or ""), "#207 key rejected")
    for row, words in (
        ({"error": "server not reachable"}, "post-editor failed: server not reachable"),
        ({"rejected_reason": "tag parity"}, "post-editor version rejected: tag parity"),
    ):
        record = qe_autofix.record(
            outcome="needs_review",
            result={"stop_reason": "fixer_failed", "rounds": [{"round": 0}, {"round": 1, **row}]},
        )
        check(words in record["message"], "#198 " + words)
        check("still below" not in record["message"], "#198 truthful failure")
    from app.services.providers import ProviderSpec

    baseline = {"score": 40, "skipped": False, "categories": {"accuracy": 2}}
    with patch.object(qe_jev_loop, "run_completion", return_value={"text": "Guten Tag"}):
        result = qe_jev_loop.refine(
            "Hello",
            "Hallo",
            "en",
            "de",
            spec=ProviderSpec(provider="mock"),
            baseline_card=baseline,
            scorer=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("judge offline")),
        )
    check(result["stop_reason"] == "fixer_failed", "L5 stop semantics unchanged")
    record = qe_autofix.record(outcome="needs_review", result=result)
    check("judge failed: judge offline" in record["message"], "L5 judge failure labelled")
    with (
        patch.object(qe_jev_loop, "run_completion", side_effect=RuntimeError("offline")) as fixer,
        patch.object(qe, "score_segment") as cross,
    ):
        result = qe_jev_loop.refine(
            "Hello",
            "Hallo",
            "en",
            "de",
            spec=ProviderSpec(provider="mock"),
            baseline_card=baseline,
            cross_check=True,
        )
    check(
        result["stop_reason"] == "fixer_failed" and fixer.call_count == 1 and not cross.called,
        "#198 failed fixer has no baseline or failed-version cross-check",
    )
    mcp_dir = Path(__file__).resolve().parents[1] / "integrations" / "mcp-server"
    sys.path.insert(0, str(mcp_dir))
    spec = importlib.util.spec_from_file_location("fx3_mcp", mcp_dir / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    msg = server._explain_status(
        httpx.Response(403, json={"error": "requires an admin-scoped user API key"})
    )
    check(
        "requires an admin-scoped user API key" in msg and "Documents scope" not in msg,
        "#208 real reason",
    )

    def local_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
        )

    with patch.object(server, "_client", local_client), patch.object(server, "TOKEN", token):
        out = json.loads(
            asyncio.run(
                server.create_workflow(
                    name="FX3 MCP", steps=[{"provider": "mock", "qe_autofix": True}]
                )
            )
        )
        check("gate is off" in str(out), "#193 MCP forwards warning")
        keyed = engines.create_profile(
            "FX3 secret",
            "openai_compatible",
            base_url="http://127.0.0.1:8078",
            api_key="fx3-private-fixture-token",  # pregate: allow-secret offline fixture
        )
        with Session(engine) as session:
            doc = Document(project_id=project.id, filename="fx3.txt")
            session.add(doc)
            session.flush()
            run = WorkflowRun(
                workflow_id=wf.id, document_id=doc.id, target_locale="de", status="failed"
            )
            session.add(run)
            session.flush()
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status="failed",
                    provider_used=f"engine:{keyed.id}",
                    error_class="RuntimeError",
                    error="failed to load model: requires about 45 GB; fx3-private-fixture-token",
                )
            )
            session.commit()
            rid = run.id
        api = client.get(f"/api/runs/{rid}")
        check(api.status_code == 200 and "requires about 45 GB" in api.text, "#215 API diagnostic")
        check("fx3-private-fixture-token" not in api.text, "#215 API secret removed")
        with Session(engine) as session:
            from sqlmodel import select

            historical = session.exec(select(StepRun).where(StepRun.workflow_run_id == rid)).one()
            historical.error = historical_error
            historical_id = historical.id
            session.add(historical)
            session.commit()
        historical_api = client.get(f"/api/runs/{rid}")
        check(
            historical_api.status_code == 200
            and "model load failed" in historical_api.text
            and "rotated-secret" not in historical_api.text
            and "[redacted]" in historical_api.text,
            "L3 historical API query credentials removed",
        )
        with Session(engine) as session:
            historical = session.get(StepRun, historical_id)
            historical.error = (
                "failed to load model: requires about 45 GB; fx3-private-fixture-token"
            )
            session.add(historical)
            session.commit()
        status = asyncio.run(server.work_status(run_id=rid))
        check(
            "requires about 45 GB" in status and "fx3-private-fixture-token" not in status,
            "#215 MCP diagnostic sanitized",
        )
    if errors:
        print("\n".join("FAIL " + message for message in errors))
        raise AssertionError(f"{len(errors)} FX3 failures")
    print("verify_fx3_messages OK")


if __name__ == "__main__":
    main()
