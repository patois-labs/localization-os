"""FX-6 spend disclosure and create preflight, offline regression tests."""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_fx6_")
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli
from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Document,
    Project,
    Segment,
    SpendLedger,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, engines, security, workflow_plan, workflows


def main():
    init_db()
    errors = []

    def check(ok, message):
        if not ok:
            errors.append(message)
            print("FAIL " + message)

    user = security.create_user("fx6@test.invalid", "FX6", "admin", "fx6-test-password")
    _, token = apikeys.create_key(user.id, "fx6", scopes="api docs admin")
    headers = {"X-Loc-Token": token}
    client = TestClient(app, headers=headers)
    mcp_dir = Path(__file__).resolve().parents[1] / "integrations" / "mcp-server"
    sys.path.insert(0, str(mcp_dir))
    spec = importlib.util.spec_from_file_location("fx6_mcp", mcp_dir / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    def local_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
        )

    with patch.object(server, "_client", local_client), patch.object(server, "TOKEN", token):
        with Session(engine) as session:
            before = len(session.exec(select(Workflow)).all())
        steps = [{"provider": "mock"} for _ in range(5)]
        steps[4]["qe_autofix_rounds"] = 2
        try:
            asyncio.run(server.create_workflow("FX6 typo", steps=steps))
        except server.ToolError as exc:
            check("steps[4]" in str(exc) and "qe_autofix_rounds" in str(exc), "#197 typo named")
        else:
            check(False, "#197 typo refused")
        with Session(engine) as session:
            check(
                len(session.exec(select(Workflow)).all()) == before, "#197 preflight writes nothing"
            )
        for malformed in ({"provider": "mock"}, [{"provider": "mock"}, None]):
            try:
                asyncio.run(server.create_workflow("FX6 malformed", steps=malformed))
            except server.ToolError:
                pass
            else:
                check(False, "#197 malformed steps refused")
        with Session(engine) as session:
            check(
                len(session.exec(select(Workflow)).all()) == before, "#197 malformed writes nothing"
            )
        partial = json.loads(
            asyncio.run(
                server.create_workflow(
                    "FX6 partial",
                    steps=[
                        {"provider": "mock"},
                        {"provider": "mock", "qe_threshold": 101},
                        {"provider": "mock"},
                    ],
                )
            )
        )
        check(
            partial.get("id") and partial["steps_added"] == 2 and partial["steps_failed"] == 1,
            "#197 server rejection keeps id and later steps",
        )
        check([r["index"] for r in partial["steps"]] == [0, 1, 2], "#197 every step accounted")

        local = engines.create_profile("FX6 local", "local", base_url="http://127.0.0.1:8077")
        judge = engines.create_profile(
            "FX6 hosted judge",
            "jev",
            api_key="fx6-fixture",  # pregate: allow-secret offline fixture
        )
        with Session(engine) as session:
            project = Project(name="FX6", source_locale="en")
            session.add(project)
            session.flush()
            doc = Document(project_id=project.id, filename="fx6.txt")
            session.add(doc)
            session.flush()
            session.add(
                Segment(document_id=doc.id, target_locale="ja", source="Hello", order_index=0)
            )
            session.add(
                Segment(document_id=doc.id, target_locale="fr", source="Hello", order_index=0)
            )
            session.commit()
            did = doc.id
            base = workflows.create_workflow(session, "FX6 default")
            variant = workflows.create_workflow(session, "FX6 Japanese")
            bid, vid = base.id, variant.id
            group = workflows.create_group(session, "FX6 group")
            workflows.add_to_group(session, bid, group.id, make_default=True)
            workflows.add_to_group(session, vid, group.id, source_locale="en", target_locale="ja")
            workflows.add_step(session, bid, provider="mock")
            workflows.add_step(
                session,
                vid,
                name="Variant translate",
                provider=f"engine:{local.id}",
                fallback_provider="claude",
                fallback_model="claude-sonnet-5",
                qe_gate=True,
                qe_provider=f"engine:{judge.id}",
                qe_autofix=True,
                qe_autofix_max_rounds=2,
                qe_autofix_cross_check=True,
            )
            workflows.add_step(
                session,
                vid,
                name="Cloud fixer",
                provider="mock",
                qe_gate=True,
                qe_provider=f"engine:{judge.id}",
                qe_autofix=True,
                qe_autofix_provider="claude",
            )
        plan = asyncio.run(
            server._plan_run_workflow(
                {"mode": "run", "workflow_id": bid, "document_id": did, "locale": "ja"}
            )
        )
        check(plan["scope"]["workflow"]["id"] == vid, "#195 resolved variant id")
        check(plan["scope"]["workflow"]["name"] == "FX6 Japanese", "#195 resolved variant name")
        rows = plan["engine"]["resolved"]
        check(len(rows) == 2, "#195 variant steps")
        if len(rows) == 2:
            check(rows[0].get("fallback", {}).get("priced") is True, "HIGH-1 priced fallback")
            check(
                rows[0].get("fallback", {}).get("model") == "claude-sonnet-5",
                "HIGH-1 fallback model",
            )
            check("fallback" not in rows[1], "HIGH-1 absent fallback omitted")
            check(rows[0]["qe_judge"]["priced"] is True, "#195 hosted judge priced")
            fix = rows[0]["auto_fix"]
            check(
                fix["provider"] == f"engine:{local.id}" and fix["priced"] is False,
                "#195 same-as-step resolves local fixer",
            )
            check(
                fix["max_rounds"] == 2 and fix["cross_check_adds_calls"],
                "#195 rounds and cross-check",
            )
            check(rows[1]["auto_fix"]["priced"] is True, "#195 cloud fixer priced")
        with patch("app.kernel.orchestrator.start_run", return_value=999):
            ack = client.post(f"/api/workflows/{bid}/run", json={"doc_id": did, "locale": "ja"})
        check(ack.json().get("workflow_id") == vid, "#195 launch acknowledgement variant")
        check(ack.json().get("workflow_name") == "FX6 Japanese", "#195 acknowledgement name")
        api = client.get(f"/api/workflows/{bid}/plan", params={"doc_id": did, "locale": "ja"})
        check(api.status_code == 200 and api.json()["workflow_id"] == vid, "#195 API preview")
        check(
            TestClient(app).get(f"/api/workflows/{bid}/plan").status_code == 401,
            "#195 auth before missing plan inputs",
        )
        check(
            client.get(f"/api/workflows/{bid}/plan").status_code == 400,
            "#195 authenticated missing plan inputs",
        )
        check(
            client.get(
                f"/api/workflows/{bid}/plan", params={"doc_id": "bad", "locale": "ja"}
            ).status_code
            == 400,
            "#195 malformed document id refused",
        )
        output = io.StringIO()
        argv = ["--json", "workflow", "run", str(did), str(bid), "ja", "--plan"]
        with contextlib.redirect_stdout(output):
            code = cli(argv)
        check(code == 0 and json.loads(output.getvalue()) == api.json(), "#195 local CLI parity")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli(argv[1:])
        check(
            code == 0 and '"fallback"' in output.getvalue() and '"claude"' in output.getvalue(),
            "HIGH-1 text CLI fallback",
        )
        output = io.StringIO()
        remote = SimpleNamespace(get=lambda path, **kw: client.get(path, **kw).json())
        with (
            patch("app.cli._remote_impl_common._client", return_value=remote),
            contextlib.redirect_stdout(output),
        ):
            code = cli(["--remote", "http://127.0.0.1", *argv])
        check(code == 0 and json.loads(output.getvalue()) == api.json(), "#195 remote CLI parity")
        web = client.get(f"/workflows/{bid}/plan", params={"doc_id": did, "locale": "ja"})
        check(
            web.status_code == 200
            and "FX6 Japanese" in web.text
            and "QE judge" in web.text
            and "up to 2 rounds" in web.text,
            "#195 web preview",
        )
        check("Fallback: claude (priced)" in web.text, "HIGH-1 web fallback")
        check(
            client.get(
                f"/api/workflows/{bid}/plan", params={"doc_id": did, "locale": "zz"}
            ).status_code
            == 400,
            "#195 nonexistent document locale refused",
        )
        check(
            client.get(
                f"/api/workflows/{bid}/plan", params={"doc_id": 999999, "locale": "ja"}
            ).status_code
            == 404,
            "#195 missing document refused",
        )

        def census():
            with Session(engine) as session:
                return [
                    len(session.exec(select(model)).all())
                    for model in (WorkflowRun, SpendLedger, AuditLog)
                ]

        before = census()
        batch_plan = json.loads(
            asyncio.run(
                server.call_tool_unvalidated(
                    "run_workflow",
                    {
                        "mode": "run_batch",
                        "workflow_id": bid,
                        "document_id": did,
                    },
                )
            )
        )
        check(census() == before, "#195 dispatcher preview makes no runs, spend or audit writes")
        check(
            {v["workflow_id"] for v in batch_plan["scope"]["variants"]} == {bid, vid},
            "#195 batch resolves each locale separately",
        )
        summary = batch_plan["scope"]["workflow"]
        check(
            all(
                value in json.dumps(summary)
                for value in ("fr", "ja", "FX6 default", "FX6 Japanese")
            ),
            "LOW-4 summary names every locale variant",
        )
        with Session(engine) as session:
            batch = WorkflowBatch(workflow_id=bid, document_id=did, requested_locales=["fr", "ja"])
            session.add(batch)
            session.flush()
            batch_id = batch.id
            for loc, status in (("fr", "failed"), ("fr", "completed"), ("ja", "failed")):
                session.add(
                    WorkflowRun(
                        workflow_id=bid,
                        document_id=did,
                        batch_id=batch_id,
                        target_locale=loc,
                        status=status,
                    )
                )
                session.flush()
            session.commit()
        before = census()
        for mode in ("rerun", "retry_failed"):
            again = json.loads(
                asyncio.run(
                    server.call_tool_unvalidated(
                        "run_workflow",
                        {
                            "mode": mode,
                            "batch_id": batch_id,
                            "locale": "ja",
                        },
                    )
                )
            )
            check(
                [v["workflow_id"] for v in again["scope"]["variants"]] == [vid],
                "#195 " + mode + " plans resolved variant, latest failed attempts only",
            )
        check(census() == before, "#195 rerun/retry preview writes nothing")
        base_plan = client.get(
            f"/api/workflows/{bid}/plan", params={"doc_id": did, "locale": "fr"}
        ).json()
        check(
            "qe_judge" not in base_plan["steps"][0] and "auto_fix" not in base_plan["steps"][0],
            "#195 disabled roles omitted",
        )
        local_judge = engines.create_profile(
            "FX6 free judge", "jev_local", base_url="http://127.0.0.1:8078"
        )
        inherited = WorkflowStep(
            workflow_id=bid,
            name="Inherited",
            provider=None,
            qe_gate=True,
            qe_provider=f"engine:{local_judge.id}",
            qe_autofix=True,
            qe_autofix_max_rounds=5,
            qe_autofix_cross_check=False,
        )
        with (
            Session(engine) as session,
            patch("app.config.DEFAULT_PROVIDER", f"engine:{local.id}"),
            patch.object(workflow_plan.orchestrator, "get_steps", return_value=[inherited]),
        ):
            disclosure = workflow_plan.plan(session, bid, session.get(Document, did), "fr")[
                "steps"
            ][0]
        check(
            disclosure["provider"] == f"engine:{local.id}" and not disclosure["priced"],
            "#195 install-default translation resolved",
        )
        check(not disclosure["qe_judge"]["priced"], "#195 local judge free")
        check(
            disclosure["auto_fix"]["provider"] == f"engine:{local.id}"
            and disclosure["auto_fix"]["max_rounds"] == 5
            and not disclosure["auto_fix"]["cross_check_adds_calls"],
            "#195 inherited fixer and cross-check off",
        )
        for ref in ("LOCAL", "Local", "local"):
            inherited.provider = ref
            with (
                Session(engine) as session,
                patch.object(workflow_plan.orchestrator, "get_steps", return_value=[inherited]),
                patch("app.config.DEFAULT_FALLBACK_PROVIDER", "claude"),
            ):
                row = workflow_plan.plan(session, bid, session.get(Document, did), "fr")["steps"][0]
            check(row["pricing"] == "local/free", "LOW-3 local case " + ref)
            check("fallback" not in row, "HIGH-1 workflow does not inherit install fallback")
        inherited.fallback_provider = "claude"
        with (
            Session(engine) as session,
            patch.object(workflow_plan.orchestrator, "get_steps", return_value=[inherited]),
        ):
            row = workflow_plan.plan(session, bid, session.get(Document, did), "fr")["steps"][0]
        check(
            not row["priced"] and row.get("fallback", {}).get("priced") is True,
            "HIGH-1 builtin local with priced fallback",
        )
        for kind in ("tm_match", "human_review", "external"):
            inherited.step_type = kind
            inherited.provider = None
            inherited.fallback_provider = "claude"
            with (
                Session(engine) as session,
                patch.object(workflow_plan.orchestrator, "get_steps", return_value=[inherited]),
            ):
                row = workflow_plan.plan(session, bid, session.get(Document, did), "fr")["steps"][0]
            check(row.get("pricing") == "no engine call", "LOW-1 no engine " + kind)
            check(
                not any(key in row for key in ("fallback", "qe_judge", "auto_fix")),
                "LOW-1 no engine roles " + kind,
            )
        with Session(engine) as session:
            archived = session.get(Workflow, bid)
            archived.archived_at = datetime.utcnow()
            session.add(archived)
            session.commit()
        archived_plan = asyncio.run(
            server._plan_run_workflow(
                {"mode": "run", "workflow_id": bid, "document_id": did, "locale": "fr"}
            )
        )
        check(
            "run would be refused" in " ".join(archived_plan["warnings"]).lower(),
            "LOW-2 archived run refusal",
        )
        archived_web = client.get(f"/workflows/{bid}/plan", params={"doc_id": did, "locale": "fr"})
        check("Run would be refused" in archived_web.text, "LOW-2 web refusal")
        check(
            client.post(
                f"/api/workflows/{bid}/run", json={"doc_id": did, "locale": "fr"}
            ).status_code
            == 400,
            "LOW-2 runtime refuses archived workflow",
        )
    if errors:
        raise AssertionError("; ".join(errors))
    print("verify_fx6_mcp OK")


if __name__ == "__main__":
    main()
